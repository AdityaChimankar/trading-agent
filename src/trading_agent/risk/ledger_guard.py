"""Read-only, realized-gross paper limits and ledger-bound research snapshots.

Not mark-to-market risk: manual fills omit fees, slippage and unrealized losses.
The API evaluates this policy inside its SQLite write transaction; scanners
bind proposals to a fingerprint and must re-scan after any ledger change.
"""
import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from contextlib import closing
from zoneinfo import ZoneInfo

from sqlalchemy.engine import make_url

from trading_agent.core.config import get_settings

COLUMNS = ("id", "symbol", "action", "entry_price", "position_size", "position_value",
           "risk_amount", "stop_loss", "take_profit", "opened_at", "status",
           "exit_price", "exit_time", "pnl")
SELECT_LEDGER = "SELECT " + ", ".join(COLUMNS) + " FROM open_positions ORDER BY id"


def utc_stamp(value):
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)


def ledger_state(rows, limits, now=None):
    """Pure projection; corrupt/missing closed-trade evidence fails closed."""
    now = now or datetime.now(UTC)
    today = now.astimezone(ZoneInfo("Asia/Kolkata")).date()
    rows = [dict(row) for row in rows]
    closed, opened = [], []
    for row in rows:
        status = str(row["status"]).lower()
        if status == "closed":
            pnl = float(row["pnl"])
            stamp = utc_stamp(row["exit_time"])
            if not math.isfinite(pnl) or stamp > now:
                raise ValueError("Invalid closed paper trade")
            closed.append((stamp, row["id"], pnl))
        elif status == "open":
            for field in ("entry_price", "position_size", "position_value", "stop_loss"):
                if not math.isfinite(float(row[field])) or float(row[field]) <= 0:
                    raise ValueError("Invalid open paper position")
            if not math.isfinite(float(row["risk_amount"])) or float(row["risk_amount"]) < 0:
                raise ValueError("Invalid open paper risk")
            opened.append(row)
        else:
            raise ValueError("Unknown paper position state")
    equity = peak = limits.initial_capital
    daily = 0.0
    for stamp, _, pnl in sorted(closed):
        equity += pnl
        if not math.isfinite(equity):
            raise ValueError("Invalid realized paper equity")
        peak = max(peak, equity)
        if stamp.astimezone(ZoneInfo("Asia/Kolkata")).date() == today:
            daily += pnl
    drawdown = max(0.0, (peak - equity) / peak)
    session_start_equity = equity - daily
    if not math.isfinite(session_start_equity) or not math.isfinite(daily):
        raise ValueError("Invalid session paper equity")
    reason = None
    if not limits.entries_enabled:
        reason = "Paper entries disabled by operator"
    elif equity <= 0 or session_start_equity <= 0:
        reason = "Paper realized capital depleted"
    elif daily <= -session_start_equity * limits.max_daily_loss_pct:
        reason = "Paper realized daily-loss limit reached"
    elif drawdown >= limits.max_drawdown_pct:
        reason = "Paper realized drawdown limit reached"
    fingerprint = hashlib.sha256(json.dumps({
        "rows": rows, "limits": limits.model_dump(), "session_date": str(today),
    }, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False).encode()).hexdigest()
    return {"fingerprint": fingerprint, "entries_allowed": reason is None, "block_reason": reason,
            "realized_equity": equity, "daily_realized_pnl": daily,
            "realized_drawdown_pct": drawdown * 100, "positions": opened,
            "accounting_basis": "realized_gross_entry_cost"}


def read_ledger_state(now=None):
    """SQLite mode=ro prevents a status GET from creating or modifying a ledger."""
    settings = get_settings()
    url = make_url(settings.database.url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        raise ValueError("Persistent SQLite ledger required for paper risk")
    uri = Path(url.database).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(SELECT_LEDGER).fetchall()
    return ledger_state(rows, settings.paper, now)
