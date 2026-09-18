"""
Monitors currently open (paper-tracked) positions: shows what they
were opened at, what they're worth right now, and an exit indicator
(HOLD or SELL) per position based on three checks, in priority order:

  1. Has price crossed the stop-loss? -> SELL (stop-loss hit)
  2. Has price crossed the take-profit? -> SELL (take-profit hit)
  3. Has the CURRENT rule-based signal for this symbol flipped to the
     opposite direction? -> SELL (signal reversed)
  4. None of the above -> HOLD

This only calculates a recommendation - it never closes a position or
places an order automatically. Closing is still a manual action (the
dashboard's Close button, or close_position() directly).
"""
from dataclasses import dataclass
from quant_indicators import latest_indicators
from portfolio_risk import get_open_positions


@dataclass
class PositionStatus:
    position: dict            # the raw open_positions row
    current_price: float
    current_value: float
    pnl: float                 # rupees, signed (positive = profit)
    pnl_pct: float
    recommendation: str        # "HOLD" or "SELL"
    reason: str


def _compute_pnl(action: str, entry_price: float, current_price: float, position_size: int) -> tuple:
    """Returns (pnl_rupees, pnl_pct). For a BUY, profit if price rose;
    for a SELL (short), profit if price fell - direction matters."""
    if action == "BUY":
        pnl_pct = (current_price - entry_price) / entry_price
    else:  # SELL (short)
        pnl_pct = (entry_price - current_price) / entry_price
    pnl_rupees = pnl_pct * entry_price * position_size
    return pnl_rupees, pnl_pct * 100


def get_position_status(position: dict, current_rule_action: str = None) -> PositionStatus | None:
    """
    position: one row from get_open_positions()
    current_rule_action: optionally pass decision_agent.decide(symbol)'s
    current action to check for signal reversal - kept as an explicit
    argument (not fetched internally) so callers can batch-fetch
    signals once for many positions instead of once per position.
    """
    indicators = latest_indicators(position["symbol"])
    if "error" in indicators:
        return None

    current_price = indicators["close"]
    action = position["action"]
    entry_price = position["entry_price"]
    position_size = position["position_size"]

    pnl, pnl_pct = _compute_pnl(action, entry_price, current_price, position_size)
    current_value = current_price * position_size

    stop_loss = position.get("stop_loss")
    take_profit = position.get("take_profit")

    recommendation, reason = "HOLD", "No exit condition met"

    if action == "BUY":
        if stop_loss is not None and current_price <= stop_loss:
            recommendation, reason = "SELL", f"Stop-loss hit (price {current_price} <= stop {stop_loss})"
        elif take_profit is not None and current_price >= take_profit:
            recommendation, reason = "SELL", f"Take-profit hit (price {current_price} >= target {take_profit})"
        elif current_rule_action == "SELL":
            recommendation, reason = "SELL", "Current rule-based signal has flipped to SELL"
    else:  # SELL (short) position
        if stop_loss is not None and current_price >= stop_loss:
            recommendation, reason = "SELL", f"Stop-loss hit (price {current_price} >= stop {stop_loss})"
        elif take_profit is not None and current_price <= take_profit:
            recommendation, reason = "SELL", f"Take-profit hit (price {current_price} <= target {take_profit})"
        elif current_rule_action == "BUY":
            recommendation, reason = "SELL", "Current rule-based signal has flipped to BUY"

    return PositionStatus(
        position=position, current_price=round(current_price, 2), current_value=round(current_value, 2),
        pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 2), recommendation=recommendation, reason=reason,
    )


def get_all_position_statuses(fetch_current_signals: bool = True) -> list:
    """Statuses for every open position. Signal-reversal checking is
    optional (fetch_current_signals=False skips it, e.g. for a quick
    check that doesn't need decision_agent's live computation)."""
    positions = get_open_positions()
    statuses = []
    for position in positions:
        current_action = None
        if fetch_current_signals:
            try:
                from decision_agent import decide
                current_action = decide(position["symbol"])["action"]
            except Exception:
                current_action = None  # don't let a signal-fetch failure hide the P&L info
        status = get_position_status(position, current_rule_action=current_action)
        if status:
            statuses.append(status)
    return statuses


if __name__ == "__main__":
    for status in get_all_position_statuses():
        p = status.position
        print(f"{p['symbol']} {p['action']} {p['position_size']}sh @ {p['entry_price']} "
              f"-> now {status.current_price} | P&L Rs.{status.pnl} ({status.pnl_pct}%) "
              f"| {status.recommendation}: {status.reason}")
