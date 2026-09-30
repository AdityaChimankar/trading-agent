"""
Paper wallet — the book of money movement behind the live /api/wallet view.

Two kinds of money in this book:
  FIXED (permanent): wallet_settings.capital, deposits, withdrawals,
    realized P&L from closed trades. These never change once written.
  FLOAT (live, mark-to-market): today's open-position P&L, which breathes
    with the latest candle and is NOT part of the fixed book.

The live wallet balance = fixed book + FLOAT open P&L:
    balance = capital
            + SUM(deposits)
            − SUM(withdrawals)
            + SUM(realized P&L)
            + today_open_pnl                    (live, refreshes 15s)

Everything else (per-day P&L, upcoming scenarios, prev-day summary) is
derived from these same tables. No forward projection — the edge analysis
showed there is no measured forward edge to project, so "upcoming" means
SCENARIO exposure around each open position's stop / target, clearly labeled.

record_trade_close() is the only place a trade's realized P&L enters the book,
and it does so atomically inside a single transaction: it computes the realized
P&L from the recorded entry vs the latest price at close time, inserts the
trades row, then inserts the matched realized_pnl wallet_transaction. A manual
close via the API always flows through here, so the wallet never misses a close.

Design note: this module does NOT auto-close anything. Closes are still manual
(the dashboard Close button). The live exit logic (stop/target/signal) is
position_monitor's recommendation only.
"""
from contextlib import contextmanager
from datetime import datetime

from storage.db import get_connection


def ensure_wallet_settings(conn) -> float:
    """Return the current paper capital. Creates the singleton row on first
    access so the book works on a fresh DB without any manual setup step."""
    row = conn.execute("SELECT id FROM wallet_settings LIMIT 1").fetchone()
    if row is None:
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO wallet_settings (id, capital, updated_at) VALUES (1, 500000.0, ?)",
            (now,),
        )
        conn.commit()
        return 500000.0
    r = conn.execute("SELECT capital, updated_at FROM wallet_settings WHERE id = 1").fetchone()
    return float(r["capital"])


def set_capital(conn, new_capital: float) -> dict:
    """Replace the paper capital. Does NOT record a transaction - it is a
    settings change, like editing an account setting. You seed capital via
    this endpoint once; after that, deposits/withdrawals move the book."""
    now = datetime.now().isoformat()
    conn.execute(
        "UPDATE wallet_settings SET capital = ?, updated_at = ? WHERE id = 1",
        (new_capital, now),
    )
    conn.commit()
    return {"capital": new_capital, "updated_at": now}


def record_deposit(conn, amount: float, note: str | None = None) -> int:
    now = datetime.now().isoformat()
    cur = conn.execute(
        "INSERT INTO wallet_transactions (type, amount, note, transacted_at) "
        "VALUES ('deposit', ?, ?, ?)",
        (amount, note, now),
    )
    conn.commit()
    return cur.lastrowid


def record_withdrawal(conn, amount: float, note: str | None = None) -> int:
    """Withdrawal is a negative amount in the same transaction table - the
    live viewer sums deposits and subtractions separately, so a withdrawal
    just looks like a negative deposit to the balance."""
    now = datetime.now().isoformat()
    cur = conn.execute(
        "INSERT INTO wallet_transactions (type, amount, note, transacted_at) "
        "VALUES ('withdrawal', ?, ?, ?)",
        (amount, note, now),
    )
    conn.commit()
    return cur.lastrowid


@contextmanager
def atomic(conn):
    """Yield the connection; on exit, commit on success, rollback on exception."""
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def _latest_close_for_symbol(conn, symbol: str) -> float | None:
    """Last known candle close for a symbol, used to mark a position to market
    at the moment it is closed (so realized P&L is computed against a price
    that actually existed, not an arbitrary number)."""
    r = conn.execute(
        "SELECT close FROM candles WHERE symbol = ? ORDER BY timestamp DESC LIMIT 1",
        (symbol,),
    ).fetchone()
    return float(r["close"]) if r and r["close"] is not None else None


def _SLIPPAGE() -> float:
    """Paper slippage model, as a fraction of the position's value (5 bps).

    Lives here rather than in the API layer so every close path - the
    positions endpoint and the wallet endpoint - deducts the same cost.
    """
    return 0.0005


def _mark(action: str, entry_price: float, mark_price: float, position_size: int) -> tuple:
    """Mark a position to an arbitrary price -> (pnl_rupees, pnl_pct).

    This is the same P&L math a real close uses, but evaluated against the
    position's stop or target instead of a live candle - it answers "what
    would this pay if the stop/target were hit?". It is a labeled scenario,
    not a prediction. position_monitor is imported locally to keep the module
    load order free of cycles.
    """
    from risk.position_monitor import _compute_pnl

    return _compute_pnl(action, entry_price, mark_price, position_size)


def record_trade_close(
    conn,
    position: dict,
    exit_reason: str,
    slippage_deducted: float = 0.0,
    exit_price_override: float | None = None,
    note: str | None = None,
) -> dict:
    """Record a closed trade AND flow the realized P&L into the wallet book,
    atomically. exit_price defaults to the latest candle close for the symbol
    (so a manually-closed paper position's P&L is computed against a real
    observed price, not whatever number you type in).

    Returns {trade_id, realized_pnl_rupees, exit_price, transaction_id}.
    """
    symbol = position["symbol"]
    if exit_price_override is None:
        exit_price = _latest_close_for_symbol(conn, symbol)
        if exit_price is None:
            raise ValueError(f"No candle price available for {symbol} - cannot mark the close to market")
    else:
        exit_price = exit_price_override

    from risk.position_monitor import _compute_pnl  # local import to avoid circular-ish cost

    pnl_rupees, _ = _compute_pnl(
        position["action"],
        float(position["entry_price"]),
        exit_price,
        int(position["position_size"]),
    )
    realized = round(pnl_rupees - slippage_deducted, 2)

    now = datetime.now().isoformat()
    with atomic(conn):
        cur = conn.execute(
            "INSERT INTO trades (symbol, action, position_id, entry_price, entry_at, "
            "exit_price, exit_at, position_size, realized_pnl_rupees, exit_reason, "
            "slippage_deducted, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (symbol,
             position["action"],
             position["id"],
             float(position["entry_price"]),
             position["opened_at"],
             exit_price,
             now,
             int(position["position_size"]),
             realized,
             exit_reason,
             round(float(slippage_deducted), 2),
             now,
            ),
        )
        trade_id = cur.lastrowid
        # Flow into the wallet book
        ttype = "realized_pnl"
        tcur = conn.execute(
            "INSERT INTO wallet_transactions (type, amount, note, symbol, trade_id, transacted_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ttype, realized, note or exit_reason, symbol, trade_id, now),
        )
        tx_id = tcur.lastrowid
        # Mark the open position closed (the real-world state change)
        conn.execute("UPDATE open_positions SET status = 'closed' WHERE id = ?", (position["id"],))

    return {"trade_id": trade_id, "realized_pnl_rupees": realized,
            "exit_price": exit_price, "transaction_id": tx_id}


def available_capital(conn) -> float:
    """The paper balance available for NEW position sizing:
    capital + deposits - withdrawals + realized P&L (the fixed book balance).
    Open-position P&L is NOT included - it is already committed to those
    positions, so counting it here would double-book money that is at work.
    Callers use this as the default `capital` for position_sizing /
    portfolio_risk instead of a hardcoded number, so sizing tracks the
    wallet book as it changes with deposits, withdrawals and closed trades.
    """
    ensure_wallet_settings(conn)
    r = conn.execute("SELECT capital FROM wallet_settings WHERE id = 1").fetchone()
    capital = float(r["capital"]) if r else 500000.0
    dep = conn.execute("SELECT COALESCE(SUM(amount), 0) AS v FROM wallet_transactions WHERE type='deposit'").fetchone()
    wd = conn.execute("SELECT COALESCE(SUM(amount), 0) AS v FROM wallet_transactions WHERE type='withdrawal'").fetchone()
    realized = conn.execute("SELECT COALESCE(SUM(amount), 0) AS v FROM wallet_transactions WHERE type='realized_pnl'").fetchone()
    return round(capital + float(dep["v"]) - float(wd["v"]) + float(realized["v"]), 2)


def wallet_snapshot(conn) -> dict:
    """Everything the live wallet endpoint needs, in one query pass.

    Returns keys:
      capital:              from wallet_settings (your seeded paper capital)
      total_deposits:       SUM of deposit amounts
      total_withdrawals:    SUM of withdrawal amounts (positive number, subtracted)
      realized_pnl_total:   SUM of realized_pnl amounts (signed, can be negative)
      open_positions:       live list with mark-to-market P&L (from the existing
                             position_monitor path; this is the live float part)
    """
    from risk.position_monitor import get_all_position_statuses

    r = conn.execute("SELECT capital FROM wallet_settings WHERE id = 1").fetchone()
    capital = float(r["capital"]) if r else 500000.0

    dep = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM wallet_transactions WHERE type='deposit'").fetchone()
    withdrawals = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM wallet_transactions WHERE type='withdrawal'").fetchone()
    realized = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM wallet_transactions WHERE type='realized_pnl'").fetchone()

    realized_pnl_total = round(float(realized["COALESCE(SUM(amount), 0)"]), 2)
    total_deposits = round(float(dep["COALESCE(SUM(amount), 0)"]), 2)
    total_withdrawals = round(float(withdrawals["COALESCE(SUM(amount), 0)"]), 2)

    return {
        "capital": capital,
        "total_deposits": total_deposits,
        "total_withdrawals": total_withdrawals,
        "realized_pnl_total": realized_pnl_total,
        "book_balance": round(capital + total_deposits - total_withdrawals + realized_pnl_total, 2),
        "open_positions": get_all_position_statuses(fetch_current_signals=True),
    }
