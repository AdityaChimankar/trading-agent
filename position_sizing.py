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

This module only calculates numbers. It does not place orders and
does not modify decision_agent.py's BUY/SELL/HOLD logic - risk sizing
is a separate concern from the decision of *whether* to trade.
"""
from dataclasses import dataclass
from quant_indicators import latest_indicators

# --- Tunable risk parameters - these are YOUR risk tolerance, not a
# recommendation. Read the module docstring below before changing them. ---
RISK_PER_TRADE_PCT = 1.0     # % of total capital risked on a single trade
ATR_STOP_MULTIPLIER = 1.5    # stop-loss = entry -/+ (this x ATR)
REWARD_RISK_RATIO = 2.0      # take-profit distance = stop distance x this
MAX_POSITION_PCT_OF_CAPITAL = 20.0  # hard cap - no single position over this % of capital,
                                      # regardless of what ATR-based sizing would otherwise allow


@dataclass
class PositionPlan:
    symbol: str
    action: str            # BUY or SELL
    entry_price: float
    atr: float
    stop_loss: float
    take_profit: float
    stop_distance: float
    risk_amount: float     # rupees risked if stop is hit
    position_size: int     # shares
    position_value: float  # rupees
    capped_by_max_position: bool  # True if MAX_POSITION_PCT_OF_CAPITAL reduced the size


def calculate_position(symbol: str, action: str, capital: float) -> PositionPlan | None:
    """
    action: "BUY" or "SELL" - use the same action decision_agent.py produced.
    capital: total capital available (rupees) - YOU supply this, the
             module never assumes or looks up an account balance.
    """
    if action not in ("BUY", "SELL"):
        return None  # no position sizing for HOLD

    indicators = latest_indicators(symbol)
    if "error" in indicators or indicators.get("atr") is None:
        return None

    entry_price = indicators["close"]
    atr = indicators["atr"]
    stop_distance = ATR_STOP_MULTIPLIER * atr

    if action == "BUY":
        stop_loss = entry_price - stop_distance
        take_profit = entry_price + stop_distance * REWARD_RISK_RATIO
    else:  # SELL
        stop_loss = entry_price + stop_distance
        take_profit = entry_price - stop_distance * REWARD_RISK_RATIO

    risk_amount = capital * (RISK_PER_TRADE_PCT / 100)
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

    return PositionPlan(
        symbol=symbol, action=action, entry_price=round(entry_price, 2), atr=round(atr, 2),
        stop_loss=round(stop_loss, 2), take_profit=round(take_profit, 2),
        stop_distance=round(stop_distance, 2), risk_amount=round(risk_amount, 2),
        position_size=position_size, position_value=round(position_size * entry_price, 2),
        capped_by_max_position=capped,
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
