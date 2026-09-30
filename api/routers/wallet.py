"""
/api/wallet — the live paper wallet.

One endpoint, one view, refreshing every 15s with the rest of the dashboard
(the positions poll). Nothing here auto-trades; everything is derived from the
paper book: wallet_settings.capital, wallet_transactions (deposits /
withdrawals / realized P&L), and open positions marked to the latest candle.

Two parts, kept separate in the response so the frontend can show them
differently:
  FIXED book  → balance, deposits, withdrawals, realized P&L, prev-day summary.
                These do not move unless you add/remove capital or close a trade.
  LIVE float  → today's open-position P&L, refreshed every 15s with price.
                This is the "real-time" part you asked for — no manual refresh.

"Upcoming day" = SCENARIO exposure for each open position:
  - current exposure (what you'd make/lose if closed now)
  - stop-hit scenario P&L (marked to the position's stop, labeled scenario)
  - target-hit scenario P&L (marked to the position's target, labeled scenario)
It is NOT a prediction. The edge analysis showed no measured forward edge to
predict from, so these are clearly-labeled what-if numbers only.
"""
from datetime import date

from fastapi import APIRouter, HTTPException, Query

from api.serializers import jsonable
from risk.portfolio_risk import exposure_summary
from risk.wallet import (
    ensure_wallet_settings, wallet_snapshot, record_deposit,
    record_trade_close, record_withdrawal, _mark, _SLIPPAGE,
)
from storage.db import get_connection

router = APIRouter(prefix="/api", tags=["wallet"])


MISSING_CAPITAL_MSG = "Paper capital not set. Initialize it with a deposit first, or set wallet_settings.capital manually."


def _today() -> str:
    return date.today().isoformat()


@router.get("/wallet")
def wallet():
    conn = get_connection()
    try:
        ensure_wallet_settings(conn)
        snap = wallet_snapshot(conn)

        # Fixed book summary. Exposure is measured against the same capital the
        # sizing gate uses (the book balance) and the same position_value sum
        # the total-exposure cap enforces (risk.portfolio_risk.exposure_summary),
        # so this view cannot disagree with what sizing will actually allow.
        fixed = {
            "capital": snap["capital"],
            "total_deposits": snap["total_deposits"],
            "total_withdrawals": snap["total_withdrawals"],
            "realized_pnl_total": snap["realized_pnl_total"],
            "book_balance": snap["book_balance"],
            **exposure_summary(snap["book_balance"]),
        }

        # Today's open P&L (live float) and scenario exposure per open position
        open_positions = snap["open_positions"]
        positions_summary: list[dict] = []
        open_current_pnl = 0.0
        for status in open_positions:
            # The monitor withholds a mark (pnl is None) when it cannot vouch for
            # one - a stale feed, or no usable candle. The wallet's numbers are
            # deliberately unchanged by that: before the monitor returned such a
            # position at all it simply dropped it, so this row contributed
            # nothing here either way. /api/positions is where it becomes visible.
            if status.pnl is None:
                continue
            entry = status.position
            stop, target = entry.get("stop_loss"), entry.get("take_profit")

            # Scenarios at current price
            cur_pnl = round(status.pnl, 2)
            cur_pnl_pct = round(status.pnl_pct, 2)

            # Stop / target scenarios (what-if, labeled)
            stop_pnl = None
            target_pnl = None
            if stop is not None:
                sp, _ = _mark(entry["action"], float(entry["entry_price"]), float(stop), int(entry["position_size"]))
                stop_pnl = round(sp, 2)
            if target is not None:
                tp, _ = _mark(entry["action"], float(entry["entry_price"]), float(target), int(entry["position_size"]))
                target_pnl = round(tp, 2)

            positions_summary.append({
                "position_id": entry["id"],
                "symbol": entry["symbol"],
                "action": entry["action"],
                "position_size": entry["position_size"],
                "entry_price": entry["entry_price"],
                "current_price": status.current_price,
                "current_value": round(status.current_value, 2),
                "current_pnl": cur_pnl,
                "current_pnl_pct": cur_pnl_pct,
                "stop_loss": stop,
                "take_profit": target,
                "stop_scenario_pnl": stop_pnl,
                "target_scenario_pnl": target_pnl,
                "at_risk_rupees": round(float(entry["risk_amount"]), 2) if entry.get("risk_amount") else None,
            })

            open_current_pnl += cur_pnl

        today_realized_pnl = _closed_pnl_today(conn)
        prev = _recent_trades_summary(conn, 7)

        return jsonable({
            "fixed": fixed,
            "today": {
                "open_pnl": round(open_current_pnl, 2),
                # Counted from the rows actually summarised, so a position the
                # monitor could not mark changes nothing here: the wallet's
                # numbers are exactly what they were before the monitor started
                # returning such a position instead of dropping it. It is
                # /api/positions - the exit surface - that shows it.
                "open_positions_count": len(positions_summary),
                "closed_pnl": today_realized_pnl,
                "total_today_pnl": round(open_current_pnl + today_realized_pnl, 2),
            },
            "open_positions": positions_summary,
            "prev_days_closed_pnl": prev,
        })
    finally:
        conn.close()


def _closed_pnl_today(conn) -> float:
    """Realized P&L from trades closed today, signed. Uses the local date the
    trades were written with (datetime.now()), not SQLite's UTC 'now', so the
    day boundary matches the exit timestamps in the table."""
    row = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl_rupees), 0) AS pnl "
        "FROM trades WHERE date(exit_at) = ?",
        (_today(),),
    ).fetchone()
    return round(float(row["pnl"]), 2)


def _recent_trades_summary(conn, days: int) -> dict:
    rows = conn.execute(
        "SELECT date(exit_at) AS d, COALESCE(SUM(realized_pnl_rupees), 0) AS pnl "
        "FROM trades WHERE typeof(exit_at) = 'text' "
        "GROUP BY d ORDER BY d DESC LIMIT ?",
        (days,),
    ).fetchall()
    summary = {}
    for row in rows:
        summary[row["d"]] = round(float(row["pnl"]), 2)
    return summary


@router.post("/wallet/close-position", status_code=200)
def close_position_wallet(position_id: int = Query(..., ge=1, description="open_positions.id to close")):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM open_positions WHERE id = ? AND status = 'open'"
            " AND symbol IS NOT NULL",
            (position_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Open position not found.")
        position = dict(row)
        result = record_trade_close(
            conn, position,
            exit_reason="manually_closed",
            slippage_deducted=round(float(position["position_value"]) * _SLIPPAGE(), 2),
        )
        return jsonable({
            "closed": position_id,
            "symbol": position["symbol"],
            "realized_pnl_rupees": result["realized_pnl_rupees"],
            "exit_price": result["exit_price"],
            "transaction_id": result["transaction_id"],
        })
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=f"Could not close position: {e}")
    finally:
        conn.close()


@router.post("/wallet/deposit", status_code=201)
def deposit(amount: float = Query(..., gt=0, description="Amount to deposit (paper).")):
    note = Query("Paper deposit", description="Note for the transaction.").default
    conn = get_connection()
    try:
        ensure_wallet_settings(conn)
        tx_id = record_deposit(conn, round(amount, 2), note)
        return {"transaction_id": tx_id, "type": "deposit", "amount": round(amount, 2)}
    finally:
        conn.close()


@router.post("/wallet/withdraw", status_code=201)
def withdraw(amount: float = Query(..., gt=0, description="Amount to withdraw (paper).")):
    note = Query("Paper withdrawal", description="Note for the transaction.").default
    conn = get_connection()
    try:
        ensure_wallet_settings(conn)
        tx_id = record_withdrawal(conn, round(amount, 2), note)
        return {"transaction_id": tx_id, "type": "withdrawal", "amount": round(amount, 2)}
    finally:
        conn.close()
