"""
Combines three independent signals into a single gated decision:
  1. Quant (RSI/ADX) - deterministic momentum + trend strength
  2. Sentiment (LLM) - news-driven bias
  3. Chart patterns - deterministic candlestick/swing patterns

evaluate_signals() is the pure rule function - both live trading
(decide()) and backtest.py call this exact same code, so backtest
results actually reflect what the live agent would have done.
"""
from datetime import datetime
from quant_indicators import latest_indicators
from pattern_detection import detect_patterns
from db import get_connection, get_watchlist_symbols


def evaluate_signals(
    rsi: float, adx: float, sentiment: float, pattern_bias: str, patterns_found: list,
    rsi_oversold: float = 30, rsi_overbought: float = 70, adx_threshold: float = 20,
    pattern_confirm_buy_rsi: float = 45, pattern_confirm_sell_rsi: float = 55,
) -> tuple:
    """Pure decision logic - no DB or I/O. Returns (action, rationale).

    Thresholds are parameterized (not hardcoded) so walk_forward_optimizer.py
    can search for better per-symbol values - the defaults here are exactly
    the values decision_agent.py and backtest.py have always used, so
    calling this with no extra arguments preserves the tested behavior
    unchanged. Only walk_forward_optimizer.py passes non-default values.
    """
    pattern_note = f", patterns: {patterns_found}" if patterns_found else ""

    # Gate: only trade when there's an actual trend (ADX) to avoid
    # chasing signals in a choppy, directionless market
    if adx < adx_threshold:
        return "HOLD", f"ADX {adx} - no clear trend, sitting out{pattern_note}"
    elif rsi < rsi_oversold and sentiment > 0.3 and pattern_bias != "bearish":
        return "BUY", f"RSI {rsi} oversold + positive sentiment ({sentiment:.2f}) + {pattern_bias} pattern bias{pattern_note}"
    elif rsi > rsi_overbought and sentiment < -0.3 and pattern_bias != "bullish":
        return "SELL", f"RSI {rsi} overbought + negative sentiment ({sentiment:.2f}) + {pattern_bias} pattern bias{pattern_note}"
    elif pattern_bias == "bullish" and rsi < pattern_confirm_buy_rsi and sentiment >= 0:
        return "BUY", f"Bullish pattern ({patterns_found}) confirmed by RSI {rsi} and non-negative sentiment"
    elif pattern_bias == "bearish" and rsi > pattern_confirm_sell_rsi and sentiment <= 0:
        return "SELL", f"Bearish pattern ({patterns_found}) confirmed by RSI {rsi} and non-positive sentiment"
    else:
        return "HOLD", f"RSI {rsi}, sentiment {sentiment:.2f}, pattern bias {pattern_bias} - no agreement"


def decide(symbol: str) -> dict:
    from sentiment import latest_sentiment  # lazy import - only live trading needs the Gemini SDK

    indicators = latest_indicators(symbol)
    if "error" in indicators:
        return {"symbol": symbol, "action": "HOLD", "rationale": indicators["error"]}

    sentiment = latest_sentiment(symbol)
    sentiment = sentiment if sentiment is not None else 0.0

    pattern_result = detect_patterns(symbol)
    pattern_bias = pattern_result["bias"]
    patterns_found = pattern_result["patterns"]
    rsi, adx = indicators["rsi"], indicators["adx"]

    action, rationale = evaluate_signals(rsi, adx, sentiment, pattern_bias, patterns_found)

    return {
        "symbol": symbol, "action": action, "rationale": rationale,
        "rsi": rsi, "adx": adx, "atr": indicators["atr"],
        "sentiment_score": sentiment,
        "patterns": ",".join(patterns_found) if patterns_found else None,
    }


def run_decision_cycle():
    """Commits per-symbol rather than once at the end of the loop -
    same reasoning as llm_decision_agent.py's fix. This path is local
    computation (no network calls) so it's much faster and less risky,
    but at 500 symbols it still adds up, and keeping the pattern
    consistent avoids reintroducing the same class of bug here."""
    timestamp = datetime.now().isoformat()
    symbols = get_watchlist_symbols()

    for symbol in symbols:
        result = decide(symbol)
        conn = get_connection()
        conn.execute(
            "INSERT INTO signals (symbol, timestamp, action, rsi, adx, atr, sentiment_score, rationale) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (symbol, timestamp, result["action"], result.get("rsi"), result.get("adx"),
             result.get("atr"), result.get("sentiment_score"), result["rationale"]),
        )
        conn.commit()
        conn.close()
        print(f"{symbol}: {result['action']} - {result['rationale']}")


if __name__ == "__main__":
    run_decision_cycle()
