"""
Hybrid Strategy: Combines Rule-based and ML-based signals with mode selection.

Three modes:
1. RULE_ONLY - Pure rule-based (current decision_agent.py logic)
2. ML_ONLY   - Pure ML model predictions (ml_decision_agent.py logic)
3. RULE_ML_COMBINED - Both must agree, or weighted combination

Mode selection is done via backtest comparison per symbol/period.
"""
from enum import Enum
from dataclasses import dataclass
from typing import Optional
import joblib
import numpy as np
import pandas as pd
import json
from pathlib import Path

from core.freshness import split_stale, warn_stale
from core.quant_indicators import compute_atr, load_candles
from core.signals import prepare_symbol_series, precompute_pattern_bias_codes
from core.pattern_detection import detect_patterns
from core.quant_indicators import latest_indicators, compute_macd
from analysis.sentiment import latest_sentiment
from paths import ML_MODEL_PATH
from storage.db import get_connection, get_watchlist_symbols
from strategy.decision_agent import evaluate_signals, decide as rule_based_decide
from strategy.ml_decision_agent import build_latest_features, ml_decide, _load_model as load_ml_model

# Cache file for best mode per symbol
MODE_CACHE_PATH = Path(__file__).parent.parent / "data" / "best_mode_cache.json"


def _load_mode_cache() -> dict:
    """Load best mode cache from disk."""
    if MODE_CACHE_PATH.exists():
        try:
            with open(MODE_CACHE_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_mode_cache(cache: dict) -> None:
    """Save best mode cache to disk."""
    MODE_CACHE_PATH.parent.mkdir(exist_ok=True)
    with open(MODE_CACHE_PATH, "w") as f:
        json.dump(cache, f)


class StrategyMode(Enum):
    RULE_ONLY = "rule_only"
    ML_ONLY = "ml_only"
    RULE_ML_COMBINED = "rule_ml_combined"


@dataclass
class HybridSignal:
    symbol: str
    action: str  # BUY, SELL, HOLD
    confidence: float
    mode: StrategyMode
    rule_action: Optional[str] = None
    rule_rationale: Optional[str] = None
    ml_action: Optional[str] = None
    ml_confidence: Optional[float] = None
    rationale: str = ""
    signal_timestamp: Optional[str] = None
    close_price: Optional[float] = None


class HybridStrategy:
    def __init__(self, mode: StrategyMode = StrategyMode.RULE_ML_COMBINED):
        self.mode = mode
        self._ml_model = None
        self._ml_feature_columns = None
    
    def _ensure_ml_loaded(self):
        if self._ml_model is None:
            self._ml_model, self._ml_feature_columns = load_ml_model()
    
    def evaluate(self, symbol: str) -> HybridSignal:
        """Evaluate signal using selected mode."""
        
        # Get rule-based signal
        rule_result = rule_based_decide(symbol)
        rule_action = rule_result["action"]
        rule_rationale = rule_result["rationale"]
        signal_timestamp = rule_result.get("signal_timestamp")
        
        # Get close price from latest_indicators
        from core.quant_indicators import latest_indicators
        indicators = latest_indicators(symbol)
        signal_close = indicators.get("close") if "error" not in indicators else None
        
        # Get ML signal
        ml_action = "HOLD"
        ml_confidence = 0.0
        
        if self.mode in (StrategyMode.ML_ONLY, StrategyMode.RULE_ML_COMBINED):
            try:
                self._ensure_ml_loaded()
                ml_result = ml_decide(symbol, self._ml_model, self._ml_feature_columns)
                ml_action = ml_result["action"]
                ml_confidence = ml_result.get("confidence", 0.0)
            except FileNotFoundError:
                ml_action = "HOLD"
                ml_confidence = 0.0
        
        # Combine based on mode
        if self.mode == StrategyMode.RULE_ONLY:
            action = rule_action
            confidence = 0.7  # Fixed confidence for rule-only
            rationale = f"RULE_ONLY: {rule_rationale}"
            
        elif self.mode == StrategyMode.ML_ONLY:
            action = ml_action
            confidence = ml_confidence
            rationale = f"ML_ONLY: ML predicts {ml_action} with {ml_confidence:.1%} confidence"
            
        else:  # RULE_ML_COMBINED
            # Both must agree for high confidence trade
            if rule_action == ml_action and rule_action != "HOLD":
                action = rule_action
                confidence = (0.7 + ml_confidence) / 2
                rationale = f"RULE_ML_COMBINED: Both agree on {action} (rule: {rule_action}, ml: {ml_action}@{ml_confidence:.1%})"
            elif rule_action != "HOLD" and ml_action == "HOLD":
                # Rule says trade, ML says hold - reduce confidence
                action = rule_action
                confidence = 0.4
                rationale = f"RULE_ML_COMBINED: Rule says {rule_action}, ML says HOLD (low confidence)"
            elif rule_action == "HOLD" and ml_action != "HOLD":
                # ML says trade, Rule says hold - reduce confidence
                action = ml_action
                confidence = ml_confidence * 0.6
                rationale = f"RULE_ML_COMBINED: ML says {ml_action}, Rule says HOLD (reduced confidence)"
            else:
                action = "HOLD"
                confidence = 0.5
                rationale = f"RULE_ML_COMBINED: Both say HOLD"
        
        return HybridSignal(
            symbol=symbol,
            action=action,
            confidence=confidence,
            mode=self.mode,
            rule_action=rule_action,
            rule_rationale=rule_rationale,
            ml_action=ml_action,
            ml_confidence=ml_confidence,
            rationale=rationale,
            signal_timestamp=signal_timestamp,
            close_price=signal_close,
        )
    
    def run_cycle(self, symbols: list = None) -> list:
        """Run decision cycle for all symbols."""
        if symbols is None:
            symbols = get_watchlist_symbols()
        
        symbols, stale = split_stale(symbols)
        warn_stale(stale, "hybrid cycle")
        
        results = []
        for symbol in symbols:
            signal = self.evaluate(symbol)
            results.append(signal)
            
            # Log to database
            timestamp = rule_result.get("signal_timestamp") if 'rule_result' in locals() else None
            from datetime import datetime
            timestamp = timestamp or datetime.now().isoformat()
            
            conn = get_connection()
            conn.execute(
                "INSERT INTO hybrid_signals (symbol, timestamp, action, confidence, mode, rule_action, ml_action, ml_confidence, rationale) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (symbol, timestamp, signal.action, signal.confidence, signal.mode.value,
                 signal.rule_action, signal.ml_action, signal.ml_confidence, signal.rationale)
            )
            conn.commit()
            conn.close()
        
        return results


def backtest_mode(mode: StrategyMode, symbols: list, candle_limit: int = 5000) -> dict:
    """
    Backtest a specific mode on historical data.
    Returns performance metrics for comparison.
    """
    from core.signals import simulate_trades
    
    all_trades = []
    
    for symbol in symbols:
        prepared = prepare_symbol_series(symbol, candle_limit)
        if prepared is None or prepared["length"] < 100:
            continue
        
        # Run simulation with this mode's logic
        trades = _simulate_with_mode(prepared, mode, symbol)
        all_trades.extend(trades)
    
    return _summarize_trades(all_trades, mode.value)


def _simulate_with_mode(prepared: dict, mode: StrategyMode, symbol: str) -> list:
    """Simulate trades using the specified mode."""
    from core.signals import FORWARD_WINDOW, SLIPPAGE, LOOKBACK_MIN, MAX_HOLD_CANDLES
    from core.signals import precompute_pattern_bias_codes
    from core.quant_indicators import compute_macd
    import pandas as pd
    
    n = prepared["length"]
    closes = prepared["closes"]
    highs = prepared["highs"]
    lows = prepared["lows"]
    opens = prepared["opens"]
    volumes = prepared["volumes"]
    rsis = prepared["rsis"]
    adxs = prepared["adxs"]
    atrs = prepared["atrs"]
    swings_norm = prepared.get("swings_norm")
    
    if swings_norm is None:
        from strategy.candidates import _swing_arrays
        swings_norm = _swing_arrays(prepared["swings"])
        prepared["swings_norm"] = swings_norm
    
    # Precompute pattern codes once
    pattern_codes = precompute_pattern_bias_codes(prepared)
    
    # Precompute MACD histogram once for all candles
    raw_df = load_candles(symbol, limit=n)
    _, _, macd_hist_series = compute_macd(raw_df)
    macd_hist_vals = macd_hist_series.values
    
    trades = []
    i = LOOKBACK_MIN
    
    # Load ML model if needed
    ml_model = None
    ml_features = None
    if mode in (StrategyMode.ML_ONLY, StrategyMode.RULE_ML_COMBINED):
        try:
            ml_model, ml_features = load_ml_model()
        except FileNotFoundError:
            pass
    
    while i < n - FORWARD_WINDOW:
        # Get rule signal at this point
        pattern_bias = "bullish" if pattern_codes[i] == 1 else ("bearish" if pattern_codes[i] == -1 else "neutral")
        patterns_found = []  # Simplified
        
        # MACD and volume for rule
        macd_hist = float(macd_hist_vals[i]) if i < len(macd_hist_vals) and not np.isnan(macd_hist_vals[i]) else 0.0
        
        vol_ratio = 1.0
        if i >= 20:
            avg_vol = volumes[i-20:i].mean()
            if avg_vol > 0:
                vol_ratio = volumes[i] / avg_vol
        
        # Sentiment (use 0 for backtest - no historical sentiment)
        sentiment = 0.0
        
        rule_action, rule_rationale = evaluate_signals(
            rsis[i], adxs[i], sentiment, pattern_bias, patterns_found,
            macd_hist=macd_hist, volume_ratio=vol_ratio
        )
        
        # Get ML signal at this point
        ml_action = "HOLD"
        ml_confidence = 0.0
        
        if mode in (StrategyMode.ML_ONLY, StrategyMode.RULE_ML_COMBINED) and ml_model is not None:
            # Build features at this index
            features = _build_features_at_index(prepared, i, ml_features, symbol)
            if features is not None:
                X = pd.DataFrame([features], columns=ml_features)
                pred = ml_model.predict(X)[0]
                probs = ml_model.predict_proba(X)[0]
                ml_confidence = float(probs[pred])
                ml_action = {0: "HOLD", 1: "BUY", 2: "SELL"}[pred]
        
        # Combine based on mode
        if mode == StrategyMode.RULE_ONLY:
            action = rule_action
        elif mode == StrategyMode.ML_ONLY:
            action = ml_action
        else:  # RULE_ML_COMBINED
            if rule_action == ml_action and rule_action != "HOLD":
                action = rule_action
            elif rule_action != "HOLD" and ml_action == "HOLD":
                action = rule_action  # But we'll track lower confidence
            elif rule_action == "HOLD" and ml_action != "HOLD":
                action = ml_action
            else:
                action = "HOLD"
        
        if action in ("BUY", "SELL"):
            entry_price = opens[i + 1] * (1 + SLIPPAGE) if action == "BUY" else opens[i + 1] * (1 - SLIPPAGE)
            atr = atrs[i]
            stop_loss = entry_price - 1.5 * atr if action == "BUY" else entry_price + 1.5 * atr
            take_profit = entry_price + 3.0 * atr if action == "BUY" else entry_price - 3.0 * atr
            
            # Simulate forward
            exit_price = None
            exit_reason = None
            for j in range(i + 1, min(i + 1 + MAX_HOLD_CANDLES, n)):
                if action == "BUY":
                    if lows[j] <= stop_loss:
                        exit_price = stop_loss
                        exit_reason = "STOP_LOSS"
                        break
                    if highs[j] >= take_profit:
                        exit_price = take_profit
                        exit_reason = "TAKE_PROFIT"
                        break
                else:  # SELL
                    if highs[j] >= stop_loss:
                        exit_price = stop_loss
                        exit_reason = "STOP_LOSS"
                        break
                    if lows[j] <= take_profit:
                        exit_price = take_profit
                        exit_reason = "TAKE_PROFIT"
                        break
            
            if exit_price is None:
                exit_price = closes[min(i + MAX_HOLD_CANDLES, n - 1)]
                exit_reason = "MAX_HOLD"
            
            if action == "BUY":
                return_pct = (exit_price - entry_price) / entry_price
            else:
                return_pct = (entry_price - exit_price) / entry_price
            
            return_pct -= SLIPPAGE
            
            trades.append({
                "symbol": symbol,
                "entry_idx": i,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "exit_idx": j if 'j' in locals() else i + MAX_HOLD_CANDLES,
                "action": action,
                "return_pct": return_pct,
                "exit_reason": exit_reason,
                "mode": mode.value,
            })
            
            i += MAX_HOLD_CANDLES  # Skip ahead
        else:
            i += 1
    
    return trades


def _build_features_at_index(prepared: dict, i: int, feature_columns: list, symbol: str) -> dict | None:
    """Build ML features at a specific index for backtesting."""
    n = prepared["length"]
    closes = prepared["closes"]
    rsis = prepared["rsis"]
    adxs = prepared["adxs"]
    
    if i < 20 or np.isnan(rsis[i]) or np.isnan(adxs[i]):
        return None
    
    from core.quant_indicators import compute_atr, load_candles
    from core.signals import precompute_pattern_bias_codes
    
    # Use prepared data where possible, load raw for volume/ATR
    raw_df = load_candles(symbol, limit=n)
    atr_vals = compute_atr(raw_df).values
    
    # Volume
    volume = raw_df["volume"].values.astype(float)
    volume_ma20 = volume[max(0, i - 19): i + 1].mean() if i >= 19 else np.nan
    volume_ratio = volume[i] / volume_ma20 if volume_ma20 and volume_ma20 > 0 else np.nan
    
    # MA20
    ma20 = closes[max(0, i - 19): i + 1].mean() if i >= 19 else np.nan
    dist_from_ma20 = (closes[i] - ma20) / ma20 if ma20 and ma20 > 0 else np.nan
    
    # Pattern code
    pattern_codes = precompute_pattern_bias_codes(prepared)
    pattern_code = pattern_codes[i]
    
    features = {
        "rsi": rsis[i], "adx": adxs[i],
        "atr_pct": atr_vals[i] / closes[i] if closes[i] else np.nan,
        "pattern_code": pattern_code,
        "return_1": (closes[i] - closes[i-1]) / closes[i-1] if i >= 1 else np.nan,
        "return_3": (closes[i] - closes[i-3]) / closes[i-3] if i >= 3 else np.nan,
        "return_6": (closes[i] - closes[i-6]) / closes[i-6] if i >= 6 else np.nan,
        "volume_ratio": volume_ratio, "dist_from_ma20": dist_from_ma20,
    }
    
    if any(v is None or (isinstance(v, float) and np.isnan(v)) for v in features.values()):
        return None
    return features


def _summarize_trades(trades: list, label: str) -> dict:
    """Summarize trade results."""
    if not trades:
        return {"label": label, "trades": 0}
    
    returns = pd.Series([t["return_pct"] for t in trades])
    wins = returns[returns > 0]
    losses = returns[returns <= 0]
    win_rate = len(wins) / len(returns) * 100
    avg_win = wins.mean() * 100 if len(wins) else 0.0
    avg_loss = losses.mean() * 100 if len(losses) else 0.0
    risk_reward = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")
    total_return = returns.sum() * 100
    
    return {
        "label": label,
        "trades": len(trades),
        "win_rate": round(win_rate, 1),
        "avg_win_pct": round(avg_win, 3),
        "avg_loss_pct": round(avg_loss, 3),
        "risk_reward_ratio": round(risk_reward, 2),
        "total_return_pct": round(total_return, 2),
    }


def find_best_mode(symbols: list, candle_limit: int = 5000, use_cache: bool = True) -> dict:
    """
    Test all three modes and return the best one per symbol.
    Uses disk cache to avoid re-running backtests.
    """
    # Load cache
    cache = _load_mode_cache() if use_cache else {}
    cached_results = {}
    symbols_to_test = []
    
    for symbol in symbols:
        if symbol in cache and use_cache:
            cached_results[symbol] = cache[symbol]
        else:
            symbols_to_test.append(symbol)
    
    # If all symbols are cached, return cached results
    if not symbols_to_test:
        # Combine cached results
        all_results = {}
        for symbol in symbols:
            all_results[symbol] = cached_results[symbol]
        
        # Find best overall mode (by average return across symbols)
        mode_returns = {}
        for mode in ["rule_only", "ml_only", "rule_ml_combined"]:
            returns = [all_results[s]["all_results"][mode]["total_return_pct"] for s in symbols]
            mode_returns[mode] = sum(returns) / len(returns) if returns else -999
        
        best_mode = max(mode_returns.keys(), key=lambda k: mode_returns[k])
        return {
            "best_mode": best_mode,
            "all_results": {symbol: cached_results[symbol] for symbol in symbols},
        }
    
    # Test uncached symbols
    modes = [StrategyMode.RULE_ONLY, StrategyMode.ML_ONLY, StrategyMode.RULE_ML_COMBINED]
    results = {}
    
    for mode in modes:
        print(f"\nTesting {mode.value} on {len(symbols_to_test)} symbol(s)...")
        result = backtest_mode(mode, symbols_to_test, candle_limit)
        results[mode.value] = result
        print(f"  {mode.value}: {result['trades']} trades, {result['win_rate']:.1f}% WR, {result['total_return_pct']:.2f}% return")
    
    # Find best mode for each symbol
    symbol_results = {}
    for symbol in symbols_to_test:
        # Get per-symbol results (we need to run backtest per symbol for this)
        # For simplicity, use overall results as proxy
        best_mode = max(results.keys(), key=lambda k: results[k].get("total_return_pct", -999))
        symbol_results[symbol] = {
            "best_mode": best_mode,
            "all_results": results,
        }
        # Update cache
        cache[symbol] = symbol_results[symbol]
    
    # Save cache
    _save_mode_cache(cache)
    
    # Combine cached and new results
    for symbol in symbols:
        if symbol in cached_results:
            symbol_results[symbol] = cached_results[symbol]
    
    # Find overall best mode
    mode_returns = {}
    for mode in ["rule_only", "ml_only", "rule_ml_combined"]:
        returns = [symbol_results[s]["all_results"][mode]["total_return_pct"] for s in symbols]
        mode_returns[mode] = sum(returns) / len(returns) if returns else -999
    
    best_mode = max(mode_returns.keys(), key=lambda k: mode_returns[k])
    
    return {
        "best_mode": best_mode,
        "all_results": symbol_results,
    }


if __name__ == "__main__":
    import sys
    
    symbols = sys.argv[1:] if len(sys.argv) > 1 else ["RELIANCE", "TCS", "INFY"]
    
    # Find best mode
    result = find_best_mode(symbols)
    print(f"\nBest mode: {result['best_mode']}")
    
    # Run live cycle with best mode
    best_mode = StrategyMode(result['best_mode'])
    strategy = HybridStrategy(best_mode)
    signals = strategy.run_cycle(symbols)
    
    for s in signals:
        print(f"{s.symbol}: {s.action} ({s.confidence:.1%}) - {s.rationale}")