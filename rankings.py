"""
Ranks the whole watchlist by bullish/bearish bias for the dashboard's
side panel. Uses the same deterministic RSI/ADX/pattern signals as
decision_agent.py - no LLM here, so this stays fast enough to run
across hundreds of symbols on every dashboard refresh.

Scoring is intentionally simple and transparent (not a black box), and
now TUNABLE via the weight constants below:
  - RSI contributes points based on how far past the 30/70 threshold
    it is, scaled by RSI_WEIGHT and shaped by RSI_CURVE
  - A matching chart pattern bias adds PATTERN_WEIGHT points
  - ADX above 20 (real trend) keeps the score full; below 20 scales it down

This is a ranking heuristic for surfacing candidates to look at, not a
trading signal itself - decision_agent.py's evaluate_signals() is still
the tested BUY/SELL/HOLD logic.
"""
from db import get_watchlist_symbols
from quant_indicators import latest_indicators
from pattern_detection import detect_patterns

# --- Tunable weights - adjust these to change how much each factor
# influences the ranking. ---
RSI_WEIGHT = 1.0
# RSI_CURVE > 1.0 makes the RSI contribution grow FASTER as RSI gets
# more extreme (e.g. RSI=5 counts disproportionately more than RSI=25),
# rewarding genuinely extreme readings over mildly-stretched ones.
# RSI_CURVE = 1.0 is linear (the old behavior). Try 1.5-2.0 to emphasize
# extremes more; use 1.0 if you want patterns to matter relatively more.
RSI_CURVE = 1.5
PATTERN_WEIGHT = 15.0
TREND_WEAK_SCALE = 0.5   # score multiplier when ADX < 20 (no confirmed trend)

RSI_MAX_DISTANCE = 30.0  # RSI distance past threshold caps here (e.g. RSI 0 or 100) - used to normalize the curve


def _rsi_component(distance: float) -> float:
    """distance is how far past the 30/70 threshold RSI is (0 if not past it).
    Normalizes to 0-1 range, applies the curve, then scales by weight -
    so RSI_CURVE changes shape without needing to also retune RSI_WEIGHT."""
    if distance <= 0:
        return 0.0
    normalized = min(distance, RSI_MAX_DISTANCE) / RSI_MAX_DISTANCE  # 0-1
    curved = normalized ** RSI_CURVE
    return curved * RSI_MAX_DISTANCE * RSI_WEIGHT


def _score_symbol(symbol: str) -> dict | None:
    indicators = latest_indicators(symbol)
    if "error" in indicators:
        return None

    rsi, adx = indicators["rsi"], indicators["adx"]
    pattern_result = detect_patterns(symbol)
    pattern_bias = pattern_result["bias"]

    bullish_distance = max(0.0, 30 - rsi) if rsi is not None else 0.0
    bearish_distance = max(0.0, rsi - 70) if rsi is not None else 0.0
    bullish_score = _rsi_component(bullish_distance)
    bearish_score = _rsi_component(bearish_distance)

    if pattern_bias == "bullish":
        bullish_score += PATTERN_WEIGHT
    elif pattern_bias == "bearish":
        bearish_score += PATTERN_WEIGHT

    if adx is not None and adx < 20:
        bullish_score *= TREND_WEAK_SCALE
        bearish_score *= TREND_WEAK_SCALE

    return {
        "symbol": symbol, "rsi": rsi, "adx": adx,
        "pattern_bias": pattern_bias, "patterns": pattern_result["patterns"],
        "bullish_score": round(bullish_score, 1), "bearish_score": round(bearish_score, 1),
    }


def rank_watchlist() -> list:
    """Scores every symbol in the watchlist. Returns raw per-symbol
    results; use top_bullish()/top_bearish() to get sorted top-N."""
    scored = []
    for symbol in get_watchlist_symbols():
        result = _score_symbol(symbol)
        if result:
            scored.append(result)
    return scored


def top_bullish(scored: list, n: int = 10) -> list:
    candidates = [s for s in scored if s["bullish_score"] > 0]
    return sorted(candidates, key=lambda s: s["bullish_score"], reverse=True)[:n]


def top_bearish(scored: list, n: int = 10) -> list:
    candidates = [s for s in scored if s["bearish_score"] > 0]
    return sorted(candidates, key=lambda s: s["bearish_score"], reverse=True)[:n]


if __name__ == "__main__":
    scored = rank_watchlist()
    print("--- Top 10 Bullish ---")
    for s in top_bullish(scored):
        print(f"  {s['symbol']}: score {s['bullish_score']}, RSI {s['rsi']}, pattern {s['pattern_bias']}")
    print("\n--- Top 10 Bearish ---")
    for s in top_bearish(scored):
        print(f"  {s['symbol']}: score {s['bearish_score']}, RSI {s['rsi']}, pattern {s['pattern_bias']}")
