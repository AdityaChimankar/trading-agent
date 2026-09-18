"""
Ranks the whole watchlist by bullish/bearish bias for the dashboard's
side panel. Uses the same deterministic RSI/ADX/pattern signals as
decision_agent.py - no LLM here, so this stays fast enough to run
across hundreds of symbols on every dashboard refresh.

Scoring is intentionally simple and transparent (not a black box), and
TUNABLE via the weight constants below:
- RSI contributes points based on how far past the 30/70 threshold
  it is, scaled by RSI_WEIGHT and shaped by RSI_CURVE
- A matching chart pattern bias adds PATTERN_WEIGHT points
- ADX above 20 (real trend) keeps the score full; below 20 scales it down
- Trend direction (price vs MA20/MA50) gates the RSI component so an
  extreme RSI reading only counts as reversal bias when it's AGAINST
  the prevailing trend, not just whenever RSI is stretched.

This module does NOT hit live prices/P&L - that stays in
position_monitor.py (which needs a Kite quote per open position). The
contradiction check below is a pure cross-reference function: the
dashboard fetches both this module's technical scores and
position_monitor.py's live P&L separately (each cached on its own TTL),
then calls annotate_contradictions() to combine them - keeping this
module fast and free of live-pricing dependencies.

This is a ranking heuristic for surfacing candidates to look at, not a
trading signal itself - decision_agent.py's evaluate_signals() is still
the tested BUY/SELL/HOLD logic.
"""

from db import get_watchlist_symbols, get_open_position_symbols
from quant_indicators import latest_indicators, load_candles
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

TREND_WEAK_SCALE = 0.5  # score multiplier when ADX < 20 (no confirmed trend)

RSI_MAX_DISTANCE = 30.0  # RSI distance past threshold caps here (e.g. RSI 0 or 100) - used to normalize the curve

TREND_MA_FAST = 20  # fast moving average period for trend-direction check
TREND_MA_SLOW = 50  # slow moving average period for trend-direction check


def _rsi_component(distance: float) -> float:
    """distance is how far past the 30/70 threshold RSI is (0 if not past it).
    Normalizes to 0-1 range, applies the curve, then scales by weight -
    so RSI_CURVE changes shape without needing to also retune RSI_WEIGHT."""
    if distance <= 0:
        return 0.0
    normalized = min(distance, RSI_MAX_DISTANCE) / RSI_MAX_DISTANCE  # 0-1
    curved = normalized ** RSI_CURVE
    return curved * RSI_MAX_DISTANCE * RSI_WEIGHT


def _trend_direction(symbol: str) -> str:
    """'up' if price is above both MAs and the fast MA is above the slow
    MA, 'down' if the mirror image is true, else 'flat' - no confirmed
    trend direction either way. Needs at least TREND_MA_SLOW candles;
    returns 'flat' (i.e. no gating) if there isn't enough history yet."""
    df = load_candles(symbol, limit=TREND_MA_SLOW + 10)
    if df is None or len(df) < TREND_MA_SLOW:
        return "flat"

    ma_fast = df["close"].rolling(TREND_MA_FAST).mean().iloc[-1]
    ma_slow = df["close"].rolling(TREND_MA_SLOW).mean().iloc[-1]
    price = df["close"].iloc[-1]

    if price > ma_fast > ma_slow:
        return "up"
    if price < ma_fast < ma_slow:
        return "down"
    return "flat"


def _score_symbol(symbol: str) -> dict | None:
    indicators = latest_indicators(symbol)
    if "error" in indicators:
        return None

    rsi, adx = indicators["rsi"], indicators["adx"]
    pattern_result = detect_patterns(symbol)
    pattern_bias = pattern_result["bias"]
    trend = _trend_direction(symbol)

    bullish_distance = max(0.0, 30 - rsi) if rsi is not None else 0.0
    bearish_distance = max(0.0, rsi - 70) if rsi is not None else 0.0

    # Gate the RSI component against confirmed trend direction: an
    # overbought reading in a confirmed uptrend is continuation, not
    # reversal, and vice versa for oversold in a confirmed downtrend.
    if trend == "up":
        bearish_distance = 0.0
    elif trend == "down":
        bullish_distance = 0.0

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
        "symbol": symbol, "rsi": rsi, "adx": adx, "trend": trend,
        "pattern_bias": pattern_bias, "patterns": pattern_result["patterns"],
        "bullish_score": round(bullish_score, 1), "bearish_score": round(bearish_score, 1),
        "has_open_position": False,   # filled in by rank_watchlist()
        "dominant_bias": None,        # filled in by annotate_contradictions()
        "contradiction": None,        # filled in by annotate_contradictions()
    }


def rank_watchlist() -> list:
    """Scores every symbol in the watchlist. Returns raw per-symbol
    results; use top_bullish()/top_bearish() to get sorted top-N."""
    open_symbols = get_open_position_symbols()
    scored = []
    for symbol in get_watchlist_symbols():
        result = _score_symbol(symbol)
        if result:
            result["has_open_position"] = symbol in open_symbols
            scored.append(result)
    return scored


def top_bullish(scored: list, n: int = 10) -> list:
    candidates = [s for s in scored if s["bullish_score"] > 0]
    return sorted(candidates, key=lambda s: s["bullish_score"], reverse=True)[:n]


def top_bearish(scored: list, n: int = 10) -> list:
    candidates = [s for s in scored if s["bearish_score"] > 0]
    return sorted(candidates, key=lambda s: s["bearish_score"], reverse=True)[:n]


def annotate_contradictions(scored: list, position_statuses: list) -> list:
    """Cross-checks each symbol's dominant technical bias against any
    open position's actual direction + live P&L. Flags a contradiction
    when the bias says one thing but the position is winning by doing
    the opposite - e.g. scored bearish here while an open LONG on that
    symbol is currently profitable.

    This is intentionally a SEPARATE pass, not baked into _score_symbol():
    rankings.py stays a pure technical read with no live-pricing
    dependency; position_monitor.py's already-fetched, already-cached
    live P&L is what makes the cross-check possible without this module
    hitting Kite quotes itself. Mutates and returns the same list.
    """
    positions_by_symbol = {s.position["symbol"]: s for s in position_statuses}

    for entry in scored:
        if entry["bullish_score"] > entry["bearish_score"]:
            entry["dominant_bias"] = "bullish"
        elif entry["bearish_score"] > entry["bullish_score"]:
            entry["dominant_bias"] = "bearish"
        else:
            entry["dominant_bias"] = "neutral"

        entry["contradiction"] = None

        status = positions_by_symbol.get(entry["symbol"])
        if status is None:
            continue

        is_long = status.position["action"] == "BUY"
        profitable = status.pnl > 0

        if entry["dominant_bias"] == "bearish" and is_long and profitable:
            entry["contradiction"] = (
                f"Flagged BEARISH here, but the open LONG is up "
                f"Rs.{status.pnl:,.0f} ({status.pnl_pct}%) - technical "
                f"read disagrees with live P&L."
            )
        elif entry["dominant_bias"] == "bullish" and not is_long and profitable:
            entry["contradiction"] = (
                f"Flagged BULLISH here, but the open SHORT is up "
                f"Rs.{status.pnl:,.0f} ({status.pnl_pct}%) - technical "
                f"read disagrees with live P&L."
            )

    return scored


def get_contradictions(scored: list) -> list:
    """Convenience filter - every symbol currently flagged as a
    contradiction, regardless of whether it's in the top-10 list."""
    return [s for s in scored if s.get("contradiction")]


if __name__ == "__main__":
    scored = rank_watchlist()
    print("--- Top 10 Bullish ---")
    for s in top_bullish(scored):
        flag = " [OPEN]" if s["has_open_position"] else ""
        print(f"  {s['symbol']}: score {s['bullish_score']}, RSI {s['rsi']}, "
              f"trend {s['trend']}, pattern {s['pattern_bias']}{flag}")

    print("\n--- Top 10 Bearish ---")
    for s in top_bearish(scored):
        flag = " [OPEN]" if s["has_open_position"] else ""
        print(f"  {s['symbol']}: score {s['bearish_score']}, RSI {s['rsi']}, "
              f"trend {s['trend']}, pattern {s['pattern_bias']}{flag}")

    # Note: contradiction checking needs live position_statuses (from
    # position_monitor.get_all_position_statuses()), which requires a
    # live Kite connection - not run here in the plain CLI entrypoint.