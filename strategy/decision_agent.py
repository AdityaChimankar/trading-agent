"""
Combines three independent signals into a single gated decision:
  1. Quant (RSI/ADX/MACD) - deterministic momentum + trend strength + trend direction
  2. Sentiment (LLM) - news-driven bias
  3. Chart patterns - deterministic candlestick/swing patterns

evaluate_signals() is the pure rule function - both live trading
(decide()) and backtest.py call this exact same code, so backtest
results actually reflect what the live agent would have done.
"""
from datetime import datetime
import pandas as pd
from core.freshness import split_stale, warn_stale
from core.pattern_detection import detect_patterns
from core.quant_indicators import latest_indicators, compute_macd
from core.logging_config import get_logger
from storage.db import get_connection, get_watchlist_symbols


logger = get_logger(__name__)


def evaluate_signals(
    rsi: float, adx: float, sentiment: float, pattern_bias: str, patterns_found: list,
    macd_hist: float = 0.0, volume_ratio: float = 1.0,
    rsi_oversold: float = 30, rsi_overbought: float = 70, adx_threshold: float = 20,
    pattern_confirm_buy_rsi: float = 50, pattern_confirm_sell_rsi: float = 50,
    macd_trend_filter: bool = False, volume_confirm: bool = False,
    min_volume_ratio: float = 1.0,
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

    action, rationale = evaluate_signals(
        rsi, adx, sentiment, pattern_bias, patterns_found,
        macd_hist=macd_hist_val, volume_ratio=vol_ratio
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
