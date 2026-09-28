"""
Ranks watchlist candidates by CONVICTION - how many of your independent
signals (technical score, ML model, LLM agent, sentiment) agree on the
same direction - rather than just the raw technical score alone.

IMPORTANT - what "conviction" is and isn't: this is NOT a prediction of
"maximum profit possible". No signal can honestly promise that - future
prices are unknown. It also isn't trying to size a bigger position for
a "better" setup: position_sizing.py deliberately risks a FIXED % of
capital per trade with a FIXED reward:risk ratio, so nearly every
suggested trade targets roughly the same rupee profit (that consistency
IS the point of risk-based sizing, not a limitation to work around).

What conviction actually measures: agreement. A setup where the
technical score, the ML model, the LLM agent, and sentiment all point
the same direction is more likely to have the FIXED profit target
actually realized than one where they disagree - that's the closest
honest proxy for "best opportunity" this system can offer. Contradicting
signals actively REDUCE conviction, the same philosophy already used by
rankings.py's contradiction check against live position P&L.

Also reports REALISTIC ROOM TO TARGET - the distance to the nearest
opposing swing high/low (resistance for a BUY, support for a SELL) -
alongside the fixed ATR-based take-profit. If the fixed target sits
beyond a real resistance/support wall, that's worth knowing; if there's
genuine room beyond it, that's a more promising setup than the fixed
target alone would suggest.
"""
from dataclasses import dataclass, field

import pandas as pd
from scipy.signal import find_peaks

from analysis.rankings import rank_watchlist_with_sizing
from core.quant_indicators import compute_atr, load_candles
from storage.db import get_connection

# Point weights for the conviction score - each confirming signal ADDS
# up to this many points (scaled by its own confidence/magnitude); each
# CONTRADICTING signal subtracts the same amount. Technical score is
# used as-is (not rescaled) so it stays consistent with what rankings.py
# already shows elsewhere.
ML_WEIGHT = 20.0
LLM_WEIGHT = 20.0
SENTIMENT_WEIGHT = 15.0
SENTIMENT_THRESHOLD = 0.15  # |sentiment| below this counts as neutral, not agreement/disagreement


@dataclass
class Opportunity:
    symbol: str
    action: str  # BUY or SELL
    conviction_score: float
    technical_score: float
    agreements: list = field(default_factory=list)
    disagreements: list = field(default_factory=list)
    entry_price: float = None
    stop_loss: float = None
    take_profit: float = None
    suggested_size: int = None
    suggested_value: float = None
    blocked: bool = False
    block_reason: str = None
    room_to_target: float = None       # rupees to the nearest opposing swing level, or None if none found
    realistic_reward_risk: float = None  # room_to_target / stop_distance, or None


def _get_latest_signal(table: str, symbol: str) -> dict | None:
    conn = get_connection()
    row = conn.execute(
        f"SELECT action, confidence FROM {table} WHERE symbol = ? ORDER BY timestamp DESC LIMIT 1",
        (symbol,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# Minimum PROMINENCE - how much a peak stands out from its surrounding
# price action, not just whether it's locally higher than nearby candles.
# This is the actual fix for the noise problem order-based local-extrema
# detection can't solve: a wider order still finds noise peaks purely by
# chance on real data (verified during development - even order=15 kept
# finding them), because "locally higher than N neighbors" says nothing
# about how SIGNIFICANT a peak is. Prominence directly measures that.
# The threshold itself is computed per-symbol as 1x ATR (see below) rather
# than a fixed percentage, so it scales with each stock's own volatility.
def _find_room_to_target(symbol: str, action: str, entry_price: float) -> float | None:
    """Distance in rupees to the nearest MEANINGFUL opposing level -
    the nearest sufficiently prominent swing HIGH above entry for a
    BUY (resistance), or swing LOW below entry for a SELL (support).
    None if no such level has been detected in the loaded history."""
    df = load_candles(symbol, limit=200)
    if len(df) < 30:
        return None

    # Prominence threshold from the stock's own volatility, so what counts
    # as a "meaningful" level scales with the stock. A NaN ATR (too few
    # warm-up candles) would make the threshold NaN and find_peaks would
    # return nothing usable or error outright - reporting no room is the
    # honest answer in that case.
    current_atr = compute_atr(df).iloc[-1]
    if pd.isna(current_atr) or current_atr <= 0:
        return None
    prominence = current_atr

    if action == "BUY":
        peak_idx, _ = find_peaks(df["high"].values, prominence=prominence)
        levels_above = [df["high"].iloc[i] for i in peak_idx if df["high"].iloc[i] > entry_price]
        return min(levels_above) - entry_price if levels_above else None
    else:  # SELL - find troughs, i.e. peaks in the INVERTED low series
        trough_idx, _ = find_peaks(-df["low"].values, prominence=prominence)
        levels_below = [df["low"].iloc[i] for i in trough_idx if df["low"].iloc[i] < entry_price]
        return entry_price - max(levels_below) if levels_below else None


def _score_conviction(symbol: str, action: str, technical_score: float) -> tuple:
    """Returns (conviction_score, agreements, disagreements)."""
    points = technical_score
    agreements, disagreements = [], []

    ml_signal = _get_latest_signal("ml_signals", symbol)
    if ml_signal and ml_signal["action"] in ("BUY", "SELL"):
        confidence = ml_signal.get("confidence") or 0.5
        if ml_signal["action"] == action:
            points += ML_WEIGHT * confidence
            agreements.append(f"ML model agrees ({action}, {confidence:.0%} confidence)")
        else:
            points -= ML_WEIGHT * confidence
            disagreements.append(f"ML model disagrees (said {ml_signal['action']}, {confidence:.0%} confidence)")

    llm_signal = _get_latest_signal("llm_signals", symbol)
    if llm_signal and llm_signal["action"] in ("BUY", "SELL"):
        confidence = llm_signal.get("confidence") or 0.5
        if llm_signal["action"] == action:
            points += LLM_WEIGHT * confidence
            agreements.append(f"LLM agent agrees ({action}, {confidence:.0%} confidence)")
        else:
            points -= LLM_WEIGHT * confidence
            disagreements.append(f"LLM agent disagrees (said {llm_signal['action']}, {confidence:.0%} confidence)")

    sentiment = None
    try:
        from analysis.sentiment import latest_sentiment  # lazy - only latest_sentiment's plain SQL read is needed here, not the Gemini SDK's scoring path
        sentiment = latest_sentiment(symbol)
    except ImportError:
        pass  # google-generativeai not installed - sentiment simply won't contribute to conviction

    if sentiment is not None and abs(sentiment) >= SENTIMENT_THRESHOLD:
        sentiment_bullish = sentiment > 0
        action_bullish = action == "BUY"
        if sentiment_bullish == action_bullish:
            points += SENTIMENT_WEIGHT * abs(sentiment)
            agreements.append(f"News sentiment agrees ({sentiment:+.2f})")
        else:
            points -= SENTIMENT_WEIGHT * abs(sentiment)
            disagreements.append(f"News sentiment disagrees ({sentiment:+.2f})")

    return round(points, 1), agreements, disagreements


def find_opportunities(capital: float, n: int = 10) -> dict:
    """Returns {'bullish': [...], 'bearish': [...]}, each a list of
    Opportunity objects sorted by conviction_score descending - the
    first entry in each list is the current top pick for that direction."""
    sized = rank_watchlist_with_sizing(capital, n=n)
    results = {"bullish": [], "bearish": []}

    for direction, key in (("bullish", "bullish_score"), ("bearish", "bearish_score")):
        for entry in sized[direction]:
            action = entry["action"]
            conviction, agreements, disagreements = _score_conviction(entry["symbol"], action, entry[key])

            room = None
            realistic_rr = None
            if entry.get("entry_price") is not None and entry.get("stop_loss") is not None:
                room = _find_room_to_target(entry["symbol"], action, entry["entry_price"])
                stop_distance = abs(entry["entry_price"] - entry["stop_loss"])
                if room is not None and stop_distance > 0:
                    realistic_rr = round(room / stop_distance, 2)

            results[direction].append(Opportunity(
                symbol=entry["symbol"], action=action, conviction_score=conviction,
                technical_score=entry[key], agreements=agreements, disagreements=disagreements,
                entry_price=entry.get("entry_price"), stop_loss=entry.get("stop_loss"),
                take_profit=entry.get("take_profit"), suggested_size=entry.get("suggested_size"),
                suggested_value=entry.get("suggested_value"), blocked=entry.get("blocked", False),
                block_reason=entry.get("block_reason"),
                room_to_target=round(room, 2) if room is not None else None,
                realistic_reward_risk=realistic_rr,
            ))

        results[direction].sort(key=lambda o: o.conviction_score, reverse=True)

    return results


if __name__ == "__main__":
    import sys
    capital = float(sys.argv[1]) if len(sys.argv) > 1 else 100000.0
    opportunities = find_opportunities(capital)
    for direction in ("bullish", "bearish"):
        print(f"--- Top {direction} ---")
        for o in opportunities[direction][:3]:
            print(f"  {o.symbol}: conviction={o.conviction_score} (technical={o.technical_score}), "
                  f"agreements={o.agreements}, disagreements={o.disagreements}")
            print(f"    entry={o.entry_price}, stop={o.stop_loss}, target={o.take_profit}, "
                  f"room_to_target={o.room_to_target}, realistic_RR={o.realistic_reward_risk}")
