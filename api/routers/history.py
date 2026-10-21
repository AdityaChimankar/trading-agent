"""
/api/transactions — the paper book's activity history.

Two tables hold this history and they answer different questions:

  wallet_transactions   the MONEY ledger: deposits, withdrawals and realized
                        P&L, in the order they moved the balance.
  trades                the TRADE record: entry/exit price, exit reason and
                        slippage, one row per closed position.

record_trade_close() in risk/wallet.py writes both inside a SINGLE transaction,
so every realized_pnl ledger row has exactly one trades row behind it. The
History page needs the ledger WITH the trade detail behind each line, which is
why both come back from one endpoint: two separate requests could land either
side of a close and render a realized_pnl line with no visible trade behind it.

Read-only. Nothing here mutates the book - depositing, withdrawing and closing
all live in api/routers/wallet.py, where they write these same tables.
"""
from fastapi import APIRouter, Query

from api.serializers import jsonable
from storage.db import get_connection

router = APIRouter(prefix="/api", tags=["history"])


@router.get("/transactions")
def transactions(limit: int = Query(200, ge=1, le=2000)):
    """The wallet ledger + closed trades + running totals, newest first.

    `summary.net` is deliberately deposits - withdrawals + realized P&L, i.e.
    the NET change the book's own trading produced. wallet_settings.capital is
    not in it: capital is the seeded starting balance, not money that moved.
    """
    conn = get_connection()
    try:
        ledger = [
            dict(r) for r in conn.execute(
                "SELECT id, type, amount, note, symbol, trade_id, transacted_at "
                "FROM wallet_transactions ORDER BY transacted_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        ]

        trades = [
            dict(r) for r in conn.execute(
                "SELECT id, symbol, action, position_id, entry_price, entry_at, "
                "       exit_price, exit_at, position_size, realized_pnl_rupees, "
                "       exit_reason, slippage_deducted "
                "FROM trades ORDER BY exit_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        ]

        money = conn.execute(
            "SELECT "
            "  COALESCE(SUM(CASE WHEN type = 'deposit' THEN amount END), 0) AS deposits, "
            "  COALESCE(SUM(CASE WHEN type = 'withdrawal' THEN amount END), 0) AS withdrawals, "
            "  COALESCE(SUM(CASE WHEN type = 'realized_pnl' THEN amount END), 0) AS realized_pnl, "
            "  COUNT(*) AS n, MIN(transacted_at) AS first_at, MAX(transacted_at) AS last_at "
            "FROM wallet_transactions"
        ).fetchone()

        # Win/loss split counts only genuinely directional outcomes - a
        # break-even close (== 0) is neither, so it can't inflate the win rate.
        stats = conn.execute(
            "SELECT "
            "  COUNT(*) AS n, "
            "  COALESCE(SUM(CASE WHEN realized_pnl_rupees > 0 THEN 1 END), 0) AS wins, "
            "  COALESCE(SUM(CASE WHEN realized_pnl_rupees < 0 THEN 1 END), 0) AS losses, "
            "  COALESCE(SUM(CASE WHEN realized_pnl_rupees > 0 THEN realized_pnl_rupees END), 0) AS gross_profit, "
            "  COALESCE(SUM(CASE WHEN realized_pnl_rupees < 0 THEN realized_pnl_rupees END), 0) AS gross_loss, "
            "  COALESCE(SUM(slippage_deducted), 0) AS slippage_total "
            "FROM trades"
        ).fetchone()

        best = conn.execute(
            "SELECT symbol, realized_pnl_rupees, exit_at FROM trades "
            "ORDER BY realized_pnl_rupees DESC LIMIT 1"
        ).fetchone()
        worst = conn.execute(
            "SELECT symbol, realized_pnl_rupees, exit_at FROM trades "
            "ORDER BY realized_pnl_rupees ASC LIMIT 1"
        ).fetchone()

        # typeof(exit_at) = 'text' guards against any legacy row written with a
        # numeric timestamp - date() on a number interprets it as a Julian day
        # and silently invents a year, the same trap the wallet router avoids.
        daily = [
            dict(r) for r in conn.execute(
                "SELECT date(exit_at) AS date, COUNT(*) AS trades, "
                "       COALESCE(SUM(realized_pnl_rupees), 0) AS closed_pnl "
                "FROM trades WHERE typeof(exit_at) = 'text' "
                "GROUP BY date ORDER BY date DESC LIMIT 30"
            ).fetchall()
        ]

        deposits = round(float(money["deposits"]), 2)
        withdrawals = round(float(money["withdrawals"]), 2)
        realized = round(float(money["realized_pnl"]), 2)

        trade_count = int(stats["n"])
        wins = int(stats["wins"])
        losses = int(stats["losses"])

        summary = {
            "deposits": deposits,
            "withdrawals": withdrawals,
            "realized_pnl": realized,
            "net": round(deposits - withdrawals + realized, 2),
            "transaction_count": int(money["n"]),
            "trade_count": trade_count,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round(wins / trade_count * 100, 1) if trade_count else None,
            "gross_profit": round(float(stats["gross_profit"]), 2),
            "gross_loss": round(float(stats["gross_loss"]), 2),
            "slippage_total": round(float(stats["slippage_total"]), 2),
            "best": dict(best) if best else None,
            "worst": dict(worst) if worst else None,
            "first_at": money["first_at"],
            "last_at": money["last_at"],
        }

        return jsonable({
            "summary": summary,
            "ledger": ledger,
            "trades": trades,
            "daily": daily,
        })
    finally:
        conn.close()
