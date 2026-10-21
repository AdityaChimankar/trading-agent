"""
Monitors currently open (paper-tracked) positions: what they were opened
at, what they're worth right now, and an exit verdict per position, from
these checks in priority order:

  1. Has the bar reached the stop-loss?          -> SELL (stop-loss)
  2. Has the bar reached the take-profit?        -> SELL (take-profit)
  3. Has the current rule-based signal flipped?  -> SELL (signal reversed)
  4. None of the above, on a fresh candle        -> HOLD
  5. None of the above, on a stale candle        -> STALE
  6. No usable candle for the symbol at all      -> NO_DATA

Two deliberate choices here, because both are ways a monitor quietly stops
protecting anything:

LEVELS ARE COMPARED AGAINST THE BAR'S LOW/HIGH, NOT ITS CLOSE. A stop is a
resting order - it fills on the first trade through the level, not on
whatever the bar eventually closed at. A minute bar that dips to 98.10 and
closes back at 98.90 traded through a 98.15 stop and got filled there;
comparing closes calls that bar a non-event, which is how the same position
gets "held" all the way down. When one bar reaches BOTH levels the stop
wins: the two are not ordered inside the bar, and assuming the favourable
one is how a simulation invents a profit that never existed.

A BREACH IS REPORTED EVEN ON AN OLD CANDLE; A HOLD IS NOT. A breach is a
positive observation about data we have - it does not become false because
the feed stalled an hour ago, so it still reports SELL and names the
candle's age. HOLD is the verdict that requires fresh data: on a stalled
feed, "no exit condition met" is indistinguishable from "we stopped
looking", so that case returns STALE instead. STALE and NO_DATA withhold
the mark - there is no price here anyone could act on, and a P&L computed
from a frozen candle is not a fact about the position. And a position is
never omitted from the list: one that disappears from the only view showing
it is one nobody can close, while still counting against the risk budget.

This module only calculates a recommendation - it never closes a position
or places an order automatically. Closing is still a manual action (the
dashboard's Close button, or close_position() directly). run_monitor_cycle()
records each non-HOLD verdict in position_alerts and logs it, so a breach
survives a closed dashboard - but it, too, closes nothing.
"""
from dataclasses import dataclass
from datetime import datetime

from core.freshness import candle_age_minutes, is_stale
from core.logging_config import get_logger
from core.quant_indicators import latest_indicators
from risk.portfolio_risk import get_open_positions
from storage.db import get_connection

logger = get_logger(__name__)


@dataclass
class PositionStatus:
    position: dict            # the raw open_positions row
    # None - never 0 - when no mark can be vouched for (STALE / NO_DATA):
    # see the module docstring. A zero here would read as "flat", which is a
    # different claim from "unknown".
    current_price: float | None
    current_value: float | None
    pnl: float | None         # rupees, signed (positive = profit)
    pnl_pct: float | None
    recommendation: str        # "HOLD" | "SELL" | "STALE" | "NO_DATA"
    reason: str
    # None for HOLD; otherwise what the verdict is ABOUT (position_alerts.kind):
    # a stop / target / reversal SELL, or "stale" / "no_data".
    alert_kind: str | None = None
    stale: bool = False                 # candle is behind the live feed during the session
    candle_age_minutes: float | None = None
    candle_timestamp: str | None = None  # the bar this verdict came from


def _compute_pnl(action: str, entry_price: float, current_price: float, position_size: int) -> tuple:
    """Returns (pnl_rupees, pnl_pct). For a BUY, profit if price rose;
    for a SELL (short), profit if price fell - direction matters."""
    if action == "BUY":
        pnl_pct = (current_price - entry_price) / entry_price
    else:  # SELL (short)
        pnl_pct = (entry_price - current_price) / entry_price
    pnl_rupees = pnl_pct * entry_price * position_size
    return pnl_rupees, pnl_pct * 100


def _level_breach(action: str, stop_loss, take_profit, high, low) -> tuple:
    """Which price LEVEL this bar reached, if any - judged on the bar's
    low/high, because that is what a resting order fills against.

    Returns (kind, reason) with kind in {"stop", "target"}, or (None, None)
    when no level was reached. The stop is checked first: one bar can reach
    both, and since the two are not ordered inside the bar, assuming the
    favourable one would book a profit the bar did not necessarily give.
    """
    if action == "BUY":
        if stop_loss is not None and low is not None and low <= stop_loss:
            return "stop", f"Stop-loss breached intrabar (low {round(float(low), 2)} <= stop {stop_loss})"
        if take_profit is not None and high is not None and high >= take_profit:
            return "target", f"Take-profit reached intrabar (high {round(float(high), 2)} >= target {take_profit})"
    else:  # SELL (short)
        if stop_loss is not None and high is not None and high >= stop_loss:
            return "stop", f"Stop-loss breached intrabar (high {round(float(high), 2)} >= stop {stop_loss})"
        if take_profit is not None and low is not None and low <= take_profit:
            return "target", f"Take-profit reached intrabar (low {round(float(low), 2)} <= target {take_profit})"
    return None, None


def get_position_status(position: dict, current_rule_action: str = None,
                        now: datetime | None = None) -> PositionStatus:
    """
    position: one row from get_open_positions()
    current_rule_action: optionally pass decision_agent.decide(symbol)'s
    current action to check for signal reversal - kept as an explicit
    argument (not fetched internally) so callers can batch-fetch
    signals once for many positions instead of once per position.
    now: the clock freshness is judged against. Defaults to the wall clock;
    callers replaying a past session (or tests) can pin it.

    Never returns None. Every open position gets a verdict, including one
    this module cannot mark, because a position missing from the list still
    counts against the risk budget while nobody can see or close it.
    """
    symbol = position["symbol"]
    indicators = latest_indicators(symbol)

    if "error" in indicators:
        return PositionStatus(
            position=position,
            current_price=None, current_value=None, pnl=None, pnl_pct=None,
            recommendation="NO_DATA",
            reason=f"No usable candle for {symbol} ({indicators['error']}) - cannot "
                   f"mark this position or judge its stop. It is still open.",
            alert_kind="no_data",
        )

    action = position["action"]
    entry_price = position["entry_price"]
    position_size = position["position_size"]

    close_price = indicators["close"]
    pnl, pnl_pct = _compute_pnl(action, entry_price, close_price, position_size)

    age = candle_age_minutes(symbol, now=now)
    stale = is_stale(symbol, now=now)
    age_text = f"{age:.0f} min old" if age is not None else "stale"

    alert_kind, reason = _level_breach(
        action, position.get("stop_loss"), position.get("take_profit"),
        indicators.get("high"), indicators.get("low"),
    )
    if alert_kind is None and current_rule_action in ("BUY", "SELL") and current_rule_action != action:
        alert_kind = "reversal"
        reason = f"Current rule-based signal has flipped to {current_rule_action}"

    if alert_kind is not None:
        # A breach is a fact about a bar that exists, so staleness qualifies it
        # rather than suppressing it - and the price is still reported, because
        # this is the one verdict the reader is being asked to act on.
        if stale:
            reason = f"{reason} (candle is {age_text} - detected late, the feed is behind)"
        return PositionStatus(
            position=position,
            current_price=round(close_price, 2),
            current_value=round(close_price * position_size, 2),
            pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 2),
            recommendation="SELL", reason=reason, alert_kind=alert_kind,
            stale=stale, candle_age_minutes=round(age, 1) if age is not None else None,
            candle_timestamp=indicators.get("timestamp"),
        )

    if stale:
        return PositionStatus(
            position=position,
            current_price=None, current_value=None, pnl=None, pnl_pct=None,
            recommendation="STALE",
            reason=f"No exit condition met, but the newest candle is {age_text} - "
                   f"HOLD cannot be confirmed while the feed is behind",
            alert_kind="stale", stale=True,
            candle_age_minutes=round(age, 1) if age is not None else None,
            candle_timestamp=indicators.get("timestamp"),
        )

    return PositionStatus(
        position=position,
        current_price=round(close_price, 2),
        current_value=round(close_price * position_size, 2),
        pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 2),
        recommendation="HOLD", reason="No exit condition met",
        stale=False, candle_age_minutes=round(age, 1) if age is not None else None,
        candle_timestamp=indicators.get("timestamp"),
    )


def get_all_position_statuses(fetch_current_signals: bool = True,
                               now: datetime | None = None) -> list:
    """Statuses for every open position - never fewer rows than
    get_open_positions() returned; anything unreadable comes back as
    NO_DATA rather than being dropped (see get_position_status).

    Signal-reversal checking is optional (fetch_current_signals=False skips
    decision_agent's live computation, e.g. for a quick check that doesn't
    need it).
    
    Positions are sorted by priority: SELL recommendations (stop/target/reversal)
    first, then STALE/NO_DATA, then HOLD. This ensures actively monitored
    positions requiring attention appear at the top.
    """
    positions = get_open_positions()
    statuses = []
    for position in positions:
        current_action = None
        if fetch_current_signals:
            try:
                from strategy.decision_agent import decide
                current_action = decide(position["symbol"])["action"]
            except Exception:
                current_action = None  # don't let a signal-fetch failure hide the P&L info
        statuses.append(get_position_status(position, current_rule_action=current_action, now=now))
    
    # Sort by priority: SELL (alert) > STALE/NO_DATA > HOLD
    def sort_key(status):
        if status.recommendation == "SELL":
            return (0, status.alert_kind or "")
        elif status.recommendation in ("STALE", "NO_DATA"):
            return (1, status.recommendation)
        else:  # HOLD
            return (2, "")
    
    statuses.sort(key=sort_key)
    return statuses


def run_monitor_cycle(now: datetime | None = None) -> dict:
    """One monitoring pass over the open book: verdict for every position,
    every non-HOLD finding written to position_alerts and logged.

    This exists because the verdict used to be computed only while a browser
    was polling /api/positions - close the dashboard and a stop breach was
    never noticed, let alone recorded. Insert-or-ignore on (position, kind,
    candle) means a breach that keeps re-firing on the same bar is recorded
    and logged once, not on every pass.

    Closes nothing: recording a breach and acting on one are deliberately
    separate steps here (see the module docstring).

    Returns {checked, alerts, recorded}; `recorded` counts only rows this
    pass actually inserted, so it doubles as "is there anything new".
    """
    statuses = get_all_position_statuses(fetch_current_signals=True, now=now)
    detected_at = (now or datetime.now()).isoformat()

    recorded = 0
    conn = get_connection()
    try:
        for status in statuses:
            if status.alert_kind is None:
                continue
            position = status.position
            cur = conn.execute(
                "INSERT OR IGNORE INTO position_alerts "
                "(position_id, symbol, kind, recommendation, candle_timestamp, "
                " price, reason, detected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (position["id"], position["symbol"], status.alert_kind,
                 status.recommendation, status.candle_timestamp, status.current_price,
                 status.reason, detected_at),
            )
            if cur.rowcount:
                recorded += 1
                logger.warning(
                    f"{position['symbol']} [{status.alert_kind}] {status.recommendation} - "
                    f"{status.reason} (position {position['id']}; recorded only, the monitor closes nothing)",
                    extra={"symbol": position["symbol"], "kind": status.alert_kind,
                           "position_id": position["id"]},
                )
        conn.commit()
    finally:
        conn.close()

    return {
        "checked": len(statuses),
        "alerts": sum(1 for s in statuses if s.alert_kind is not None),
        "recorded": recorded,
    }


if __name__ == "__main__":
    for status in get_all_position_statuses():
        p = status.position
        mark = "no mark" if status.pnl is None else f"-> {status.current_price} | P&L Rs.{status.pnl} ({status.pnl_pct}%)"
        print(f"{p['symbol']} {p['action']} {p['position_size']}sh @ {p['entry_price']} "
              f"{mark} | {status.recommendation}: {status.reason}")
