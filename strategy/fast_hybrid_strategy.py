"""
Fast Hybrid Strategy for Real-Time Trading.

Optimizations:
1. Uses precomputed indicator cache (updated by background process)
2. Skips LLM sentiment in hot path (reads cached sentiment)
3. Shares prepared data between rule + ML evaluation
4. Loads ML model once at startup
5. Incremental updates instead of full recomputation
"""
from enum import Enum
from dataclasses import dataclass
from typing import Optional
import joblib
import numpy as np
import pandas as pd
import json
import time
from pathlib import Path
from threading import Lock

from core.freshness import split_stale, warn_stale
from core.quant_indicators import compute_atr, load_candles
from core.signals import (
    prepare_symbol_series, precompute_pattern_bias_codes,
    LOOKBACK_MIN, MAX_HOLD_CANDLES, FORWARD_WINDOW, SLIPPAGE
)
from core.pattern_detection import detect_patterns
from core.quant_indicators import latest_indicators, compute_macd
from paths import ML_MODEL_PATH
from storage.db import get_connection, get_watchlist_symbols
from strategy.decision_agent import evaluate_signals, decide as rule_based_decide
from strategy.ml_decision_agent import _load_model as load_ml_model


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
    latency_ms: float = 0.0


class IndicatorCache:
    """Thread-safe cache for precomputed indicators with disk persistence."""
    
    def __init__(self, max_age_seconds: int = 3600, cache_dir: str = None):  # 1 hour default
        self._cache: dict[str, dict] = {}
        self._timestamps: dict[str, float] = {}
        self._lock = Lock()
        self.max_age = max_age_seconds
        # Use project root / data / indicator_cache
        from paths import PROJECT_ROOT
        self.cache_dir = Path(cache_dir) if cache_dir else PROJECT_ROOT / "data" / "indicator_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
    
    def _cache_path(self, symbol: str) -> Path:
        return self.cache_dir / f"{symbol}.pkl"
    
    def get(self, symbol: str) -> Optional[dict]:
        with self._lock:
            if symbol in self._cache:
                age = time.time() - self._timestamps[symbol]
                if age <= self.max_age:
                    return self._cache[symbol]
                else:
                    # Expired
                    del self._cache[symbol]
                    del self._timestamps[symbol]
            
            # Try loading from disk
            path = self._cache_path(symbol)
            if path.exists():
                try:
                    import pickle
                    file_age = time.time() - path.stat().st_mtime
                    if file_age <= self.max_age:
                        with open(path, "rb") as f:
                            data = pickle.load(f)
                        self._cache[symbol] = data
                        self._timestamps[symbol] = time.time()
                        return data
                except Exception:
                    pass
            return None
    
    def set(self, symbol: str, data: dict) -> None:
        with self._lock:
            self._cache[symbol] = data
            self._timestamps[symbol] = time.time()
            # Persist to disk
            try:
                import pickle
                with open(self._cache_path(symbol), "wb") as f:
                    pickle.dump(data, f)
            except Exception:
                pass
    
    def invalidate(self, symbol: str) -> None:
        with self._lock:
            self._cache.pop(symbol, None)
            self._timestamps.pop(symbol, None)
            path = self._cache_path(symbol)
            if path.exists():
                path.unlink()
    
    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self._timestamps.clear()
            for path in self.cache_dir.glob("*.pkl"):
                path.unlink()

    def load_all_from_disk(self) -> int:
        """Load all valid cache files from disk into memory.
        
        Returns number of symbols loaded.
        """
        count = 0
        for path in self.cache_dir.glob("*.pkl"):
            symbol = path.stem
            if symbol not in self._cache:
                try:
                    import pickle
                    file_age = time.time() - path.stat().st_mtime
                    if file_age <= self.max_age:
                        with open(path, "rb") as f:
                            data = pickle.load(f)
                        self._cache[symbol] = data
                        self._timestamps[symbol] = time.time()
                        count += 1
                except Exception:
                    pass
        return count


class SentimentCache:
    """Thread-safe cache for sentiment scores (updated async) with disk persistence."""
    
    def __init__(self, max_age_seconds: int = 1800, cache_dir: str = None):  # 30 min default
        self._cache: dict[str, float] = {}
        self._timestamps: dict[str, float] = {}
        self._lock = Lock()
        self.max_age = max_age_seconds
        # Use project root / data / sentiment_cache
        from paths import PROJECT_ROOT
        self.cache_dir = Path(cache_dir) if cache_dir else PROJECT_ROOT / "data" / "sentiment_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
    
    def _cache_path(self) -> Path:
        return self.cache_dir / "sentiment.json"
    
    def get(self, symbol: str) -> float:
        with self._lock:
            if symbol in self._cache:
                age = time.time() - self._timestamps[symbol]
                if age <= self.max_age:
                    return self._cache[symbol]
            return 0.0  # Neutral default
    
    def set(self, symbol: str, score: float) -> None:
        with self._lock:
            self._cache[symbol] = score
            self._timestamps[symbol] = time.time()
    
    def update_from_db(self) -> None:
        """Bulk load latest sentiment from database."""
        conn = get_connection()
        try:
            rows = conn.execute("""
                SELECT s.symbol, s.score 
                FROM sentiment s
                JOIN news n ON s.news_id = n.id
                WHERE n.published_at >= datetime('now', '-1 day')
            """).fetchall()
            for row in rows:
                self.set(row["symbol"], row["score"])
            # Persist to disk
            import json
            with open(self._cache_path(), "w") as f:
                json.dump(self._cache, f)
        finally:
            conn.close()
    
    def load_from_disk(self) -> None:
        """Load sentiment cache from disk."""
        path = self._cache_path()
        if path.exists():
            try:
                import json
                with open(path) as f:
                    data = json.load(f)
                self._cache = data
                self._timestamps = {k: time.time() for k in data}
            except Exception:
                pass

    def load_all_from_disk(self) -> int:
        """Load sentiment cache from disk (alias for load_from_disk)."""
        self.load_from_disk()
        return len(self._cache)


# Global caches (shared across strategy instances)
_indicator_cache = IndicatorCache()
_sentiment_cache = SentimentCache()

# Load from disk on module import
_indicator_cache.load_all_from_disk()
_sentiment_cache.load_from_disk()
_ml_model = None
_ml_feature_columns = None


def _ensure_ml_loaded():
    """Load ML model once at module level."""
    global _ml_model, _ml_feature_columns
    if _ml_model is None:
        _ml_model, _ml_feature_columns = load_ml_model()


def _get_prepared_data(symbol: str) -> dict:
    """Get precomputed indicator data from cache or compute on-demand."""
    cached = _indicator_cache.get(symbol)
    if cached is not None:
        return cached
    
    # On-demand computation (fallback)
    prepared = prepare_symbol_series(symbol)
    if prepared:
        _indicator_cache.set(symbol, prepared)
    return prepared


def _get_cached_sentiment(symbol: str) -> float:
    """Get cached sentiment (no LLM call in hot path)."""
    return _sentiment_cache.get(symbol)


def _build_ml_features_from_prepared(prepared: dict, i: int, feature_columns: list, symbol: str) -> dict | None:
    """Build ML features from precomputed prepared data (no DB loads)."""
    n = prepared["length"]
    closes = prepared["closes"]
    rsis = prepared["rsis"]
    adxs = prepared["adxs"]
    
    if i < 20 or np.isnan(rsis[i]) or np.isnan(adxs[i]):
        return None
    
    # ATR from prepared data (already computed)
    atr_vals = prepared["atrs"]
    
    # Volume from prepared data
    volumes = prepared["volumes"]
    volume_ma20 = volumes[max(0, i - 19): i + 1].mean() if i >= 19 else np.nan
    volume_ratio = volumes[i] / volume_ma20 if volume_ma20 and volume_ma20 > 0 else np.nan
    
    # MA20 from closes
    ma20 = closes[max(0, i - 19): i + 1].mean() if i >= 19 else np.nan
    dist_from_ma20 = (closes[i] - ma20) / ma20 if ma20 and ma20 > 0 else np.nan
    
    # Pattern code from precomputed
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


class FastHybridStrategy:
    """Fast hybrid strategy for real-time trading.
    
    Uses precomputed caches, skips LLM sentiment, shares data between
    rule and ML evaluation. Target: < 50ms per symbol.
    """
    
    def __init__(self, mode: StrategyMode = StrategyMode.RULE_ML_COMBINED,
                 indicator_cache: IndicatorCache = None,
                 sentiment_cache: SentimentCache = None):
        self.mode = mode
        self._indicator_cache = indicator_cache or _indicator_cache
        self._sentiment_cache = sentiment_cache or _sentiment_cache
        
        # Load ML model once
        _ensure_ml_loaded()
        self._ml_model = _ml_model
        self._ml_feature_columns = _ml_feature_columns
    
    def evaluate(self, symbol: str) -> HybridSignal:
        """Evaluate signal using precomputed data (fast path)."""
        start_time = time.perf_counter()
        
        # Get precomputed indicators
        prepared = _get_prepared_data(symbol)
        if prepared is None or prepared["length"] < LOOKBACK_MIN:
            return HybridSignal(
                symbol=symbol, action="HOLD", confidence=0.0,
                mode=self.mode, rationale="No indicator data available"
            )
        
        i = prepared["length"] - 1  # Latest candle index
        
        # Get rule-based signal using cached data
        rule_action, rule_rationale = self._evaluate_rule_from_prepared(prepared, i, symbol)
        
        # Get ML signal using cached data
        ml_action = "HOLD"
        ml_confidence = 0.0
        
        if self.mode in (StrategyMode.ML_ONLY, StrategyMode.RULE_ML_COMBINED) and self._ml_model is not None:
            features = _build_ml_features_from_prepared(
                prepared, i, self._ml_feature_columns, symbol
            )
            if features is not None:
                import pandas as pd
                X = pd.DataFrame([features], columns=self._ml_feature_columns)
                pred = self._ml_model.predict(X)[0]
                probs = self._ml_model.predict_proba(X)[0]
                ml_confidence = float(probs[pred])
                ml_action = {0: "HOLD", 1: "BUY", 2: "SELL"}[pred]
        
        # Combine based on mode
        if self.mode == StrategyMode.RULE_ONLY:
            action = rule_action
            confidence = 0.7
            rationale = f"RULE_ONLY: {rule_rationale}"
            
        elif self.mode == StrategyMode.ML_ONLY:
            action = ml_action
            confidence = ml_confidence
            rationale = f"ML_ONLY: ML predicts {ml_action} with {ml_confidence:.1%} confidence"
            
        else:  # RULE_ML_COMBINED
            if rule_action == ml_action and rule_action != "HOLD":
                action = rule_action
                confidence = (0.7 + ml_confidence) / 2
                rationale = f"RULE_ML_COMBINED: Both agree on {action}"
            elif rule_action != "HOLD" and ml_action == "HOLD":
                action = rule_action
                confidence = 0.4
                rationale = f"RULE_ML_COMBINED: Rule says {rule_action}, ML says HOLD"
            elif rule_action == "HOLD" and ml_action != "HOLD":
                action = ml_action
                confidence = ml_confidence * 0.6
                rationale = f"RULE_ML_COMBINED: ML says {ml_action}, Rule says HOLD"
            else:
                action = "HOLD"
                confidence = 0.5
                rationale = f"RULE_ML_COMBINED: Both say HOLD"
        
        latency_ms = (time.perf_counter() - start_time) * 1000
        
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
            latency_ms=latency_ms
        )
    
    def _evaluate_rule_from_prepared(self, prepared: dict, i: int, symbol: str) -> tuple:
        """Evaluate rule-based signal using precomputed data (no DB loads)."""
        # Get indicators from prepared data
        rsi = prepared["rsis"][i]
        adx = prepared["adxs"][i]
        atr = prepared["atrs"][i]
        
        # Pattern bias from precomputed
        pattern_codes = precompute_pattern_bias_codes(prepared)
        pattern_bias = "bullish" if pattern_codes[i] == 1 else ("bearish" if pattern_codes[i] == 2 else "neutral")
        
        # MACD - compute from prepared closes (fast)
        closes = prepared["closes"]
        if i >= 26:
            ema12 = pd.Series(closes).ewm(span=12, adjust=False).mean()
            ema26 = pd.Series(closes).ewm(span=26, adjust=False).mean()
            macd_line = ema12 - ema26
            signal_line = macd_line.ewm(span=9, adjust=False).mean()
            macd_hist = float(macd_line.iloc[i] - signal_line.iloc[i])
        else:
            macd_hist = 0.0
        
        # Volume ratio from prepared
        volumes = prepared["volumes"]
        vol_ratio = prepared.get("volume_ratio", np.ones_like(volumes))[i]
        if np.isnan(vol_ratio):
            vol_ratio = 1.0
        
        # Cached sentiment (no LLM call)
        sentiment = _get_cached_sentiment(symbol)
        
        # Evaluate
        patterns_found = []  # Simplified - could extract from prepared
        return evaluate_signals(
            rsi, adx, sentiment, pattern_bias, patterns_found,
            macd_hist=macd_hist, volume_ratio=vol_ratio
        )
    
    def run_cycle(self, symbols: list = None) -> list:
        """Run decision cycle for all symbols (fast)."""
        if symbols is None:
            symbols = get_watchlist_symbols()
        
        symbols, stale = split_stale(symbols)
        warn_stale(stale, "fast hybrid cycle")
        
        results = []
        for symbol in symbols:
            signal = self.evaluate(symbol)
            results.append(signal)
            
            # Log to database (async in production)
            self._log_signal(signal)
        
        return results
    
    def _log_signal(self, signal: HybridSignal) -> None:
        """Log signal to database."""
        from datetime import datetime
        timestamp = datetime.now().isoformat()
        
        conn = get_connection()
        conn.execute(
            "INSERT INTO hybrid_signals (symbol, timestamp, action, confidence, mode, rule_action, ml_action, ml_confidence, rationale) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (signal.symbol, timestamp, signal.action, signal.confidence, signal.mode.value,
             signal.rule_action, signal.ml_action, signal.ml_confidence, signal.rationale)
        )
        conn.commit()
        conn.close()


def precompute_all_indicators(symbols: list = None, candle_limit: int = 5000, skip_cached: bool = True) -> dict:
    """Background job: precompute indicators for all symbols.
    
    Run this every 5 minutes via scheduler.
    Uses smaller candle_limit (5000) for faster warmup.
    
    Args:
        symbols: List of symbols to precompute (default: all watchlist)
        candle_limit: Number of candles to load (default: 5000 for speed)
        skip_cached: If True, skip symbols already in cache (default: True)
    """
    if symbols is None:
        symbols = get_watchlist_symbols()
    
    results = {}
    for symbol in symbols:
        if skip_cached and _indicator_cache.get(symbol) is not None:
            results[symbol] = "cached"
            continue
            
        try:
            prepared = prepare_symbol_series(symbol, candle_limit)
            if prepared:
                _indicator_cache.set(symbol, prepared)
                results[symbol] = "ok"
            else:
                results[symbol] = "insufficient_data"
        except Exception as e:
            results[symbol] = f"error: {e}"
    
    return results


def update_sentiment_cache() -> dict:
    """Background job: update sentiment cache from database.
    
    Run this every 15-30 minutes via scheduler.
    """
    _sentiment_cache.update_from_db()
    return {"updated": len(_sentiment_cache._cache)}


def warmup_caches(symbols: list = None) -> dict:
    """Warm up all caches at startup."""
    if symbols is None:
        symbols = get_watchlist_symbols()
    
    print(f"Warming up caches for {len(symbols)} symbols...")
    
    # Precompute indicators
    indicator_results = precompute_all_indicators(symbols)
    
    # Load sentiment
    _sentiment_cache.update_from_db()
    
    # Load ML model
    _ensure_ml_loaded()
    
    return {
        "indicators": indicator_results,
        "sentiment_loaded": len(_sentiment_cache._cache),
        "ml_model_loaded": _ml_model is not None
    }


def warmup_caches_for_symbols(symbols: list) -> dict:
    """Warm up caches only for specific symbols (fast)."""
    print(f"Warming up caches for {len(symbols)} symbols...")
    
    indicator_results = precompute_all_indicators(symbols)
    
    # Ensure sentiment and ML are loaded
    _sentiment_cache.update_from_db()
    _ensure_ml_loaded()
    
    return {
        "indicators": indicator_results,
        "sentiment_loaded": len(_sentiment_cache._cache),
        "ml_model_loaded": _ml_model is not None
    }


if __name__ == "__main__":
    import sys
    
    symbols = sys.argv[1:] if len(sys.argv) > 1 else None
    
    # Warm up caches
    warmup_result = warmup_caches(symbols)
    print(f"Warmup: {warmup_result}")
    
    # Run fast cycle
    best_mode = StrategyMode.RULE_ML_COMBINED  # Could load from cache
    strategy = FastHybridStrategy(best_mode)
    signals = strategy.run_cycle(symbols)
    
    total_latency = sum(s.latency_ms for s in signals)
    print(f"\nProcessed {len(signals)} symbols in {total_latency:.1f}ms total ({total_latency/len(signals):.1f}ms/symbol)")
    
    for s in signals:
        print(f"{s.symbol}: {s.action} ({s.confidence:.1%}) [{s.latency_ms:.1f}ms] - {s.rationale}")