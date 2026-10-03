"""
Combines three independent signals into a single gated decision:
  1. Quant (RSI/ADX/MACD) - deterministic momentum + trend strength + trend direction
  2. Sentiment (LLM) - news-driven bias
  3. Chart patterns - deterministic candlestick/swing patterns

evaluate_signals() is the pure rule function - both live trading
(decide()) and backtest.py call this exact same code, so backtest
results actually reflect what the live agent would have done.

NEW: Optional session/regime gates from strategy/session_features.py and
strategy/candidates.py - these are the "when NOT to trade" filters that
measured positive in the strategy lab. Enabled via config.
"""
from datetime import datetime
import pandas as pd
from core.freshness import split_stale, warn_stale
from core.pattern_detection import detect_patterns
from core.quant_indicators import latest_indicators, compute_macd
from core.logging_config import get_logger
from storage.db import get_connection, get_watchlist_symbols
from core.config import get_config


logger = get_logger(__name__)


def evaluate_signals(
    rsi: float, adx: float, sentiment: float, pattern_bias: str, patterns_found: list,
    macd_hist: float = 0.0, volume_ratio: float = 1.0,
    rsi_oversold: float = 30, rsi_overbought: float = 70, adx_threshold: float = 20,
    pattern_confirm_buy_rsi: float = 50, pattern_confirm_sell_rsi: float = 50,
    macd_trend_filter: bool = False, volume_confirm: bool = False,
    min_volume_ratio: float = 1.0,
    # Session/regime gates (optional, from strategy/candidates.py)
    session_window: bool = False, session_open_minute: int = 585, session_close_minute: int = 870,
    cost_cover: bool = False, cost_cover_multiple: float = 3.0,
    vol_regime: bool = False, vol_regime_low: float = 0.2, vol_regime_high: float = 0.8,
    vwap_filter: bool = False, vwap_rising: bool = False,
    htf_agreement: bool = False,
    gap_filter: bool = False, gap_max_pct: float = 0.02,
    # Current candle context for session gates
    minute_of_day: int = None, atr_pctile: float = None, dist_vwap: float = None,
    vwap_slope: float = None, htf_fast_ret: float = None, htf_slow_ret: float = None,
    gap_pct: float = None, expected_move: float = None,
) -> tuple:
    """Pure decision logic - no DB or I/O. Returns (action, rationale).

    Thresholds are parameterized (not hardcoded) so walk_forward_optimizer.py
    can search for better per-symbol values - the defaults here are exactly
    the values decision_agent.py and backtest.py have always used, so
    calling this with no extra arguments preserves the tested behavior
    unchanged. Only walk_forward_optimizer.py passes non-default values.
    """
    pattern_note = f", patterns: {patterns_found}" if patterns_found else ""

    # Gate 1: only trade when there's an actual trend (ADX) to avoid
    # chasing signals in a choppy, directionless market
    if adx < adx_threshold:
        return "HOLD", f"ADX {adx:.1f} < {adx_threshold} - no clear trend, sitting out{pattern_note}"

    # Gate 2: MACD trend direction filter - only trade WITH the trend
    if macd_trend_filter:
        if macd_hist > 0 and pattern_bias == "bearish":
            return "HOLD", f"MACD bullish ({macd_hist:.3f}) conflicts with bearish pattern{pattern_note}"
        if macd_hist < 0 and pattern_bias == "bullish":
            return "HOLD", f"MACD bearish ({macd_hist:.3f}) conflicts with bullish pattern{pattern_note}"

    # Gate 3: Volume confirmation - require above-average volume
    if volume_confirm and volume_ratio < min_volume_ratio:
        return "HOLD", f"Volume ratio {volume_ratio:.2f} < {min_volume_ratio} - weak conviction{pattern_note}"

    # Session/regime gates - the "when NOT to trade" layer
    # These are applied BEFORE entry logic to filter out unfavorable conditions
    if session_window:
        if minute_of_day is None or not (session_open_minute <= minute_of_day <= session_close_minute):
            return "HOLD", f"Outside session window ({session_open_minute//60:02d}:{session_open_minute%60:02d}-{session_close_minute//60:02d}:{session_close_minute%60:02d}){pattern_note}"

    if cost_cover:
        if expected_move is None or expected_move < cost_cover_multiple * 0.0005:  # SLIPPAGE
            return "HOLD", f"Expected move {expected_move:.4f} < {cost_cover_multiple}x cost{pattern_note}"

    if vol_regime:
        if atr_pctile is None or not (vol_regime_low <= atr_pctile <= vol_regime_high):
            return "HOLD", f"ATR percentile {atr_pctile:.2f} outside [{vol_regime_low}, {vol_regime_high}]{pattern_note}"

    if vwap_filter:
        if dist_vwap is None or dist_vwap < 0:
            return "HOLD", f"Price below VWAP (dist {dist_vwap:.4f}){pattern_note}"

    if vwap_rising:
        if vwap_slope is None or vwap_slope < 0:
            return "HOLD", f"VWAP not rising (slope {vwap_slope:.4f}){pattern_note}"

    if htf_agreement:
        if htf_fast_ret is None or htf_slow_ret is None or htf_fast_ret <= 0 or htf_slow_ret <= 0:
            return "HOLD", f"HTF disagreement (fast {htf_fast_ret:.4f}, slow {htf_slow_ret:.4f}){pattern_note}"

    if gap_filter:
        if gap_pct is not None and abs(gap_pct) >= gap_max_pct:
            return "HOLD", f"Large gap {gap_pct:.2%} >= {gap_max_pct:.2%}{pattern_note}"

    # Strategy 1: RSI oversold/overbought with sentiment confirmation (live only)
    if rsi < rsi_oversold and sentiment > 0.3 and pattern_bias != "bearish":
        return "BUY", f"RSI {rsi:.1f} oversold + positive sentiment ({sentiment:.2f}) + {pattern_bias} pattern{pattern_note}"
    elif rsi > rsi_overbought and sentiment < -0.3 and pattern_bias != "bullish":
        return "SELL", f"RSI {rsi:.1f} overbought + negative sentiment ({sentiment:.2f}) + {pattern_bias} pattern{pattern_note}"

    # Strategy 2: Pattern-confirmed entries with tighter RSI confluence
    # BUY: bullish pattern + RSI not overbought + MACD bullish or neutral
    if pattern_bias == "bullish" and rsi < pattern_confirm_buy_rsi and sentiment >= 0:
        if not macd_trend_filter or macd_hist >= 0:
            return "BUY", f"Bullish pattern ({patterns_found}) + RSI {rsi:.1f} < {pattern_confirm_buy_rsi} + MACD {macd_hist:.3f}{pattern_note}"

    # SELL: bearish pattern + RSI not oversold + MACD bearish or neutral
    if pattern_bias == "bearish" and rsi > pattern_confirm_sell_rsi and sentiment <= 0:
        if not macd_trend_filter or macd_hist <= 0:
            return "SELL", f"Bearish pattern ({patterns_found}) + RSI {rsi:.1f} > {pattern_confirm_sell_rsi} + MACD {macd_hist:.3f}{pattern_note}"

    return "HOLD", f"RSI {rsi:.1f}, ADX {adx:.1f}, MACD {macd_hist:.3f}, vol {volume_ratio:.2f}, sentiment {sentiment:.2f}, pattern {pattern_bias} - no agreement"


def decide(symbol: str) -> dict:
    from analysis.sentiment import latest_sentiment  # lazy import - only live trading needs the LLM sentiment path
    from strategy.session_features import features as session_features
    from core.signals import prepare_symbol_series

    indicators = latest_indicators(symbol)
    if "error" in indicators:
        return {"symbol": symbol, "action": "HOLD", "rationale": indicators["error"]}

    sentiment = latest_sentiment(symbol)
    sentiment = sentiment if sentiment is not None else 0.0

    pattern_result = detect_patterns(symbol)
    pattern_bias = pattern_result["bias"]
    patterns_found = pattern_result["patterns"]
    rsi, adx, atr = indicators["rsi"], indicators["adx"], indicators["atr"]

    # Compute MACD histogram for trend direction filter
    from core.quant_indicators import load_candles, compute_macd
    df = load_candles(symbol, limit=50)
    _, _, macd_hist = compute_macd(df)
    macd_hist_val = float(macd_hist.iloc[-1]) if len(macd_hist) > 0 and not pd.isna(macd_hist.iloc[-1]) else 0.0

    # Compute volume ratio (current vs 20-period average)
    vol_ratio = 1.0
    if len(df) >= 20:
        avg_vol = df["volume"].rolling(20).mean().iloc[-1]
        curr_vol = df["volume"].iloc[-1]
        if avg_vol > 0:
            vol_ratio = float(curr_vol / avg_vol)

    # Get session/regime features for optional gates
    config = get_config()
    signal_cfg = config.signal
    
    minute_of_day = None
    atr_pctile = None
    dist_vwap = None
    vwap_slope = None
    htf_fast_ret = None
    htf_slow_ret = None
    gap_pct = None
    expected_move = None
    
    if signal_cfg.use_session_gates:
        try:
            prepared = prepare_symbol_series(symbol, candle_limit=2000)
            if prepared:
                sf = session_features(prepared)
                i = prepared["length"] - 1  # latest candle
                minute_of_day = float(sf["minute_of_day"][i]) if i < len(sf["minute_of_day"]) else None
                atr_pctile = float(sf["atr_pctile"][i]) if i < len(sf["atr_pctile"]) else None
                dist_vwap = float(sf["dist_vwap"][i]) if i < len(sf["dist_vwap"]) else None
                vwap_slope = float(sf["vwap_slope"][i]) if i < len(sf["vwap_slope"]) else None
                htf_fast_ret = float(sf["htf_fast_ret"][i]) if i < len(sf["htf_fast_ret"]) else None
                htf_slow_ret = float(sf["htf_slow_ret"][i]) if i < len(sf["htf_slow_ret"]) else None
                gap_pct = float(sf["gap_pct"][i]) if i < len(sf["gap_pct"]) else None
                expected_move = float(sf["expected_move"][i]) if i < len(sf["expected_move"]) else None
        except Exception:
            pass  # If session features fail, continue without them

    action, rationale = evaluate_signals(
        rsi, adx, sentiment, pattern_bias, patterns_found,
        macd_hist=macd_hist_val, volume_ratio=vol_ratio,
        # Session/regime gates from config
        session_window=signal_cfg.use_session_gates,
        session_open_minute=signal_cfg.session_gate_open_minute,
        session_close_minute=signal_cfg.session_gate_close_minute,
        cost_cover=signal_cfg.use_session_gates,
        cost_cover_multiple=signal_cfg.cost_cover_multiple,
        vol_regime=signal_cfg.use_session_gates,
        vol_regime_low=signal_cfg.vol_regime_low_pctile,
        vol_regime_high=signal_cfg.vol_regime_high_pctile,
        vwap_filter=signal_cfg.use_session_gates,
        vwap_rising=signal_cfg.use_session_gates,
        htf_agreement=signal_cfg.use_session_gates,
        gap_filter=signal_cfg.use_session_gates,
        gap_max_pct=signal_cfg.gap_max_pct,
        # Session feature values
        minute_of_day=minute_of_day,
        atr_pctile=atr_pctile,
        dist_vwap=dist_vwap,
        vwap_slope=vwap_slope,
        htf_fast_ret=htf_fast_ret,
        htf_slow_ret=htf_slow_ret,
        gap_pct=gap_pct,
        expected_move=expected_move,
    )

    return {
        "symbol": symbol, "action": action, "rationale": rationale,
        "rsi": rsi, "adx": adx, "atr": atr,
        "sentiment_score": sentiment,
        "patterns": ",".join(patterns_found) if patterns_found else None,
        "macd_hist": macd_hist_val,
        "volume_ratio": vol_ratio,
        # Timestamp of the candle this decision was made on (None if the
        # indicator read failed) - see run_decision_cycle().
        "signal_timestamp": indicators.get("timestamp"),
    }


def run_decision_cycle():
    """Commits per-symbol rather than once at the end of the loop -
    same reasoning as llm_decision_agent.py's fix. This path is local
    computation (no network calls) so it's much faster and less risky,
    but at 500 symbols it still adds up, and keeping the pattern
    consistent avoids reintroducing the same class of bug here."""
    symbols = get_watchlist_symbols()

    # Never decide on a price that no longer exists - see core/freshness.py.
    symbols, stale = split_stale(symbols)
    warn_stale(stale, "decision cycle")

    for symbol in symbols:
        result = decide(symbol)
        # Log against the candle the decision came from, not wall-clock
        # now(). Signals were previously written at arbitrary seconds
        # (09:17:23) while candles are minute buckets (09:15:00), so the
        # dashboard chart could never line a signal up with its bar.
        # Falls back to now() only when the indicator read failed.
        timestamp = result.get("signal_timestamp") or datetime.now().isoformat()
        conn = get_connection()
        conn.execute(
            "INSERT INTO signals (symbol, timestamp, action, rsi, adx, atr, sentiment_score, rationale) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (symbol, timestamp, result["action"], result.get("rsi"), result.get("adx"),
             result.get("atr"), result.get("sentiment_score"), result["rationale"]),
        )
        conn.commit()
        conn.close()
        logger.info(
            f"{symbol}: {result['action']} - {result['rationale']}",
            extra={"symbol": symbol, "action": result["action"], "rsi": result.get("rsi"), "adx": result.get("adx")},
        )


if __name__ == "__main__":
    from core.logging_config import setup_logging
    setup_logging()
    run_decision_cycle()
