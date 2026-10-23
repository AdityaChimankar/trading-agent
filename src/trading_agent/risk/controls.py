"""Intraday trading controls: kill switch, session gates, square-off, audit trail.

These sit in front of every new entry (paper or live) and complement the
realized-loss limits in ``ledger_guard``:

- Kill switch: a persisted halt that blocks all new entries until an operator
  resumes. It survives restarts because it lives on disk, not in memory.
- Session gates: no entries before the open or after ``entry_cutoff``, and at
  most ``max_trades_per_day`` entries per IST trading day.
- Square-off: at ``square_off_time`` every open position is closed at the
  latest stored price, the way a broker auto-squares MIS positions.
- Audit log: an append-only JSONL record of every entry, exit, halt and
  resume, one file per IST day, for after-the-fact review.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from filelock import FileLock

from trading_agent.core.config import get_settings
from trading_agent.core.logging_config import get_logger

logger = get_logger(__name__)
IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = time(9, 15)
_audit_lock = threading.Lock()


def _controls_dir() -> Path:
    path = get_settings().data_dir / "controls"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _parse_hhmm(value: str) -> time:
    hours, minutes = (int(part) for part in value.split(":"))
    return time(hours, minutes)


# --- audit ---------------------------------------------------------------------

def audit(event: str, **details: Any) -> dict:
    """Append one event to today's audit file and return it."""
    now = datetime.now(UTC)
    record = {"id": uuid4().hex, "at": now.isoformat(), "event": event, **details}
    path = _controls_dir() / f"audit-{now.astimezone(IST).date().isoformat()}.jsonl"
    line = json.dumps(record, default=str, allow_nan=False)
    with _audit_lock, FileLock(str(path) + ".lock"):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    return record


def read_audit(day: str | None = None, limit: int = 200) -> list[dict]:
    """Newest-first audit events for one IST day (default today)."""
    day = day or datetime.now(IST).date().isoformat()
    path = _controls_dir() / f"audit-{day}.jsonl"
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            events.append({"event": "corrupt_audit_line"})
    return events[::-1][:limit]


# --- kill switch -----------------------------------------------------------------

def _halt_path() -> Path:
    return _controls_dir() / "kill_switch.json"


def halt_state() -> dict:
    path = _halt_path()
    if not path.exists():
        return {"halted": False, "reason": None, "since": None, "by": None}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return {"halted": bool(state.get("halted")), "reason": state.get("reason"),
                "since": state.get("since"), "by": state.get("by")}
    except ValueError:
        # An unreadable switch fails closed: treat it as engaged.
        return {"halted": True, "reason": "Kill-switch file unreadable", "since": None, "by": None}


def _write_halt(state: dict) -> None:
    path = _halt_path()
    tmp = path.with_suffix(f".{uuid4().hex}.tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(path)


def engage_kill_switch(reason: str, by: str = "operator") -> dict:
    state = {"halted": True, "reason": reason, "since": datetime.now(UTC).isoformat(), "by": by}
    _write_halt(state)
    audit("kill_switch_engaged", reason=reason, by=by)
    logger.warning("kill_switch_engaged", reason=reason, by=by)
    return state


def release_kill_switch(by: str = "operator") -> dict:
    state = {"halted": False, "reason": None, "since": datetime.now(UTC).isoformat(), "by": by}
    _write_halt(state)
    audit("kill_switch_released", by=by)
    return halt_state()


# --- session gates -----------------------------------------------------------------

def entries_today(rows: list[dict], now: datetime | None = None) -> int:
    """Positions opened on the current IST trading day (open or since closed)."""
    today = (now or datetime.now(UTC)).astimezone(IST).date()
    count = 0
    for row in rows:
        opened = row.get("opened_at")
        if opened is None:
            continue
        stamp = opened if isinstance(opened, datetime) else datetime.fromisoformat(str(opened))
        stamp = stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp
        count += stamp.astimezone(IST).date() == today
    return count


def entry_block_reason(rows: list[dict], now: datetime | None = None) -> str | None:
    """Why a new entry is not allowed right now, or None if it is."""
    now = now or datetime.now(UTC)
    limits = get_settings().paper
    halt = halt_state()
    if halt["halted"]:
        return f"Kill switch engaged: {halt['reason'] or 'no reason given'}"
    local = now.astimezone(IST)
    if limits.enforce_session:
        if local.weekday() >= 5:
            return "Market closed (weekend)"
        if local.time() < MARKET_OPEN:
            return "Before market open (09:15 IST)"
        if local.time() >= _parse_hhmm(limits.entry_cutoff):
            return f"After the {limits.entry_cutoff} IST entry cutoff"
    if entries_today(rows, now) >= limits.max_trades_per_day:
        return f"Daily trade limit reached ({limits.max_trades_per_day})"
    return None


def square_off_due(now: datetime | None = None) -> bool:
    local = (now or datetime.now(UTC)).astimezone(IST)
    return local.weekday() < 5 and local.time() >= _parse_hhmm(get_settings().paper.square_off_time)


def session_status(rows: list[dict], now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    limits = get_settings().paper
    return {
        "now_ist": now.astimezone(IST).isoformat(),
        "kill_switch": halt_state(),
        "entry_block_reason": entry_block_reason(rows, now),
        "entries_today": entries_today(rows, now),
        "max_trades_per_day": limits.max_trades_per_day,
        "entry_cutoff": limits.entry_cutoff,
        "square_off_time": limits.square_off_time,
        "square_off_due": square_off_due(now),
    }


async def square_off_all(reason: str = "intraday_square_off") -> list[dict]:
    """Close every open paper position at its latest stored close.

    Positions with no stored price are left open and reported, never closed
    at a made-up price.
    """
    from sqlalchemy import select

    from trading_agent.execution.router import get_order_router
    from trading_agent.storage.db import Position, get_database

    async with get_database().session() as session:
        open_positions = list((await session.execute(
            select(Position).where(Position.status.in_(["open", "OPEN"])))).scalars())
    results = []
    router = get_order_router()
    for position in open_positions:
        try:
            result = await router.exit_position(position.id, reason=reason)
            results.append({"position_id": position.id, "symbol": position.symbol, "status": "closed", **result})
        except Exception as error:  # report and keep squaring off the rest
            results.append({"position_id": position.id, "symbol": position.symbol,
                            "status": "failed", "error": str(error)})
    audit("square_off", reason=reason, results=results)
    return results
