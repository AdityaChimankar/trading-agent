"""
ATR-based position sizing and stop-loss/take-profit - fills the gap
flagged in README.md: "No position sizing / stop-loss logic yet (ATR
is computed but not used for this) - add before considering any real
capital."

Core idea: risk a FIXED, SMALL percentage of your capital per trade
(not a fixed number of shares), with the stop-loss distance set by
the stock's own recent volatility (ATR) rather than an arbitrary
percentage. A volatile stock gets a wider stop and smaller size; a
calm stock gets a tighter stop and larger size - both risk the same
rupee amount.

TWO MEASURED CORRECTIONS to the original version, both from
research/diagnose_edge.py --exits and research/strategy_lab.py:

  1. NO TAKE-PROFIT BY DEFAULT (USE_TAKE_PROFIT = False). The fixed 2:1
     target measured -0.0451%/trade against -0.0363% for the stop alone -
     the single largest improvement available in this project, and it is a
     subtraction. See the constant's comment for the full numbers and the
     mechanism.
  2. VOLATILITY-AWARE RISK SCALING. Position size is scaled DOWN when a
     name's ATR sits in the top of its own recent range, because that is
     exactly when a 1.5xATR stop falls inside a single bar and the planned
     risk stops being the realisable risk. It can only reduce risk, never
     increase it.

This module only calculates numbers. It does not place orders and
does not modify decision_agent.py's BUY/SELL/HOLD logic - risk sizing
is a separate concern from the decision of *whether* to trade.

CONSUMERS: take_profit is now Optional. risk/position_monitor.py already
treats None as "no target level" and falls back to the stop and the
signal-reversal exit, and the open_positions.take_profit column is
nullable, so no downstream change was needed - but a new consumer that
assumes a float must handle None.
"""
from dataclasses import dataclass

import pandas as pd

from core.quant_indicators import latest_indicators

# --- Tunable risk parameters - these are YOUR risk tolerance, not a
# recommendation. Read the module docstring below before changing them. ---
RISK_PER_TRADE_PCT = 1.0     # % of total capital risked on a single trade
ATR_STOP_MULTIPLIER = 1.5    # stop-loss = entry -/+ (this x ATR)
REWARD_RISK_RATIO = 2.0      # take-profit distance = stop distance x this
MAX_POSITION_PCT_OF_CAPITAL = 20.0  # hard cap - no single position over this % of capital,
                                      # regardless of what ATR-based sizing would otherwise allow

# Whether to place a take-profit at all. THIS IS THE LARGEST MEASURED
# IMPROVEMENT AVAILABLE IN THE PROJECT, and it is a subtraction, not an
# addition.
#
# research/diagnose_edge.py --exits walked every signal forward bar-by-bar to
# whichever level was touched first, and found the fixed 2:1 take-profit
# DESTROYS the stop-only result:
#
#     1.5xATR stop + 2:1 target   -0.0451% net per trade   <- what this module used to do
#     1.5xATR stop, no target     -0.0363% net per trade   <- +0.0088%/trade better
#     3.0xATR stop + 2:1 target   (no better than stop-only)
#
# research/strategy_lab.py reproduces the same gap independently on every
# candidate it measures - e.g. the VWAP candidate returns +0.0166% net
# stop-only and -0.0167% net WITH the target. The target is not merely
# unhelpful, it flips a positive number negative.
#
# Why: a 6-bar hold on a 1.5xATR stop reaches a 2:1 target only when the
# move is ~3x the stop distance, which almost never happens inside the hold
# window. So the target mostly converts small favourable drifts into
# capped exits while the stop still takes the full loss on the rest -
# a worse payoff distribution than letting the stop alone decide. (The
# numbers above are still NET NEGATIVE. Removing the target is a smaller
# loss, not a profitable strategy.)
#
# Set to True to restore the old stop+target behaviour, e.g. to re-measure it.
USE_TAKE_PROFIT = False

# Volatility-aware risk scaling. In the top of a name's own recent
# volatility range, a 1.5xATR stop is inside a single bar's range: the fill
# assumption breaks down and the realized loss exceeds the planned one. So
# risk is scaled DOWN when volatility is unusually high for that name.
#
# This can only ever reduce risk (the multiplier is capped at 1.0), because
# the failure mode of "sizing up in a quiet regime" is far worse than the
# failure mode of "sizing down in a violent one".
VOLATILITY_RISK_CUTOFF = 0.80   # ATR percentile above which risk starts scaling down
VOLATILITY_RISK_FLOOR = 0.50    # never scale below half the base risk
VOLATILITY_WINDOW_CANDLES = 500  # candles loaded to compute the percentile (see below)


@dataclass
class PositionPlan:
    symbol: str
    action: str            # BUY or SELL
    entry_price: float
    atr: float
    stop_loss: float
    take_profit: float | None
    stop_distance: float
    risk_amount: float     # rupees risked if stop is hit
    position_size: int     # shares
    position_value: float  # rupees
    capped_by_max_position: bool  # True if MAX_POSITION_PCT_OF_CAPITAL reduced the size
    volatility_pctile: float | None = None   # ATR percentile of the trailing window
    risk_multiplier: float = 1.0             # volatility scaling actually applied

def round_to_tick(price: float, tick_size: float = 0.05) -> float:
    """Rounds a price to the nearest valid NSE tick size."""
    return round(round(price / tick_size) * tick_size, 2)


def volatility_risk_multiplier(atr_pctile: float | None) -> float:
    """Risk multiplier in [VOLATILITY_RISK_FLOOR, 1.0] from an ATR percentile.

    Flat at 1.0 below VOLATILITY_RISK_CUTOFF, then falling linearly to
    VOLATILITY_RISK_FLOOR at the 100th percentile. Returns 1.0 whenever the
    percentile is unknown - sizing must never silently change because a
    feature failed to compute.
    """
    if atr_pctile is None or atr_pctile != atr_pctile:  # None or NaN
        return 1.0
    if atr_pctile <= VOLATILITY_RISK_CUTOFF:
        return 1.0
    span = 1.0 - VOLATILITY_RISK_CUTOFF
    falloff = (atr_pctile - VOLATILITY_RISK_CUTOFF) / span
    return max(VOLATILITY_RISK_FLOOR, 1.0 - falloff * (1.0 - VOLATILITY_RISK_FLOOR))


def current_volatility_percentile(symbol: str) -> float | None:
    """ATR-as-%-of-price percentile for a symbol's trailing window, or None.

    Reuses the SAME feature the candidate gates read
    (strategy/session_features.py) so sizing and entry cannot disagree about
    what "high volatility" means - the same single-definition discipline
    research/backtest.py documents for DEFAULT_PARAMS.

    Returns None - not a guess - when history is too short for a percentile.
    Callers then size at the unmultiplied base risk, which is the safe
    direction (a missed discount costs opportunity, a fabricated one costs
    capital).
    """
    from core.quant_indicators import compute_atr, load_candles

    try:
        df = load_candles(symbol, limit=VOLATILITY_WINDOW_CANDLES)
    except Exception:
        return None
    if len(df) < 100:
        return None

    atr_pct = compute_atr(df) / df["close"]
    pctile = (
        atr_pct.rolling(
            min(len(df) - 1, 250), min_periods=60
        ).rank(pct=True).iloc[-1]
    )
    return float(pctile) if pd.notna(pctile) else None


def calculate_position(symbol: str, action: str, capital: float,
                       use_take_profit: bool | None = None) -> PositionPlan | None:
    """
    action: "BUY" or "SELL" - use the same action decision_agent.py produced.
    capital: total capital available (rupees) - YOU supply this, the
             module never assumes or looks up an account balance.
    use_take_profit: override the module-level USE_TAKE_PROFIT for one call.
    """
    if action not in ("BUY", "SELL"):
        return None  # no position sizing for HOLD

    indicators = latest_indicators(symbol)
    if "error" in indicators or indicators.get("atr") is None:
        return None

    entry_price = indicators["close"]
    atr = indicators["atr"]
    stop_distance = ATR_STOP_MULTIPLIER * atr

    want_target = USE_TAKE_PROFIT if use_take_profit is None else use_take_profit
    take_profit = None
    if want_target:
        target_distance = stop_distance * REWARD_RISK_RATIO
        take_profit = entry_price + target_distance if action == "BUY" else entry_price - target_distance

    stop_loss = entry_price - stop_distance if action == "BUY" else entry_price + stop_distance

    # Scale the rupee risk by the name's current volatility regime BEFORE
    # converting it to a share count, so the multiplier affects the position
    # size and not just a reported number.
    atr_pctile = current_volatility_percentile(symbol)
    multiplier = volatility_risk_multiplier(atr_pctile)
    risk_amount = capital * (RISK_PER_TRADE_PCT / 100) * multiplier
    raw_size = int(risk_amount / stop_distance) if stop_distance > 0 else 0

    # Hard cap: never let position value exceed MAX_POSITION_PCT_OF_CAPITAL,
    # even if ATR-based sizing on a very tight stop would suggest a huge
    # position. This protects against the case where ATR is unusually
    # small (e.g. a quiet pre-open period) and would otherwise size up
    # aggressively.
    max_position_value = capital * (MAX_POSITION_PCT_OF_CAPITAL / 100)
    max_size_by_cap = int(max_position_value / entry_price) if entry_price > 0 else 0
    capped = raw_size > max_size_by_cap
    position_size = min(raw_size, max_size_by_cap)

    # Stop-loss and take-profit are snapped to a valid NSE tick size so
    # the plan shows prices that could actually be placed; entry stays the
    # raw last price.
    return PositionPlan(
        symbol=symbol, action=action,
        entry_price=round(entry_price, 2),
        atr=round(atr, 2),
        stop_loss=round_to_tick(stop_loss),
        take_profit=round_to_tick(take_profit) if take_profit is not None else None,
        stop_distance=round(stop_distance, 2),
        risk_amount=round(risk_amount, 2),
        position_size=position_size,
        position_value=round(position_size * entry_price, 2),
        capped_by_max_position=capped,
        volatility_pctile=round(atr_pctile, 3) if atr_pctile is not None else None,
        risk_multiplier=round(multiplier, 3),
    )


if __name__ == "__main__":
    import sys
    symbol = sys.argv[1] if len(sys.argv) > 1 else "RELIANCE"
    action = sys.argv[2] if len(sys.argv) > 2 else "BUY"
    capital = float(sys.argv[3]) if len(sys.argv) > 3 else 100000.0

    plan = calculate_position(symbol, action, capital)
    if plan is None:
        print(f"Could not calculate a position for {symbol} - not enough candle data, or action was HOLD.")
    else:
        print(f"--- Position plan: {symbol} {action} ---")
        for field, value in plan.__dict__.items():
            print(f"  {field}: {value}")
