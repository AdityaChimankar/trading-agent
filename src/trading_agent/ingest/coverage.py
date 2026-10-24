"""Candle coverage: is every stored symbol complete up to the latest closed session?

There is no exchange holiday calendar yet (see MEMORY.md), so the calendar is
every IST date on which any checked symbol has a bar. A weekday with no bars at
all is therefore either a holiday or a day nothing was fetched; ``check`` flags
the store as stale only when its newest session is older than the last weekday
whose market has closed.

A session is complete when it has the 09:15 bar and a bar starting within one
bar of 15:15. Since NSE's closing auction (2026-08-03) many stocks have no
continuous-trading bars after 15:14, so a later bar is not required. Today's
session is required only after 15:30 IST.

Thinly traded stocks often have no trade at 09:15 or near 15:14, so their bars
never look complete. Each successful Kite fetch therefore records the window it
covered in a per-symbol manifest (``record_fetch``); a session counts as covered
when its bars are complete or the whole session was fetched. That also stops
``backfill --missing-only`` from re-requesting the same thin-stock sessions.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd

IST = "Asia/Kolkata"
SESSION_OPEN_MINUTE, SESSION_EXIT_MINUTE, SESSION_CLOSE_MINUTE = 9 * 60 + 15, 15 * 60 + 15, 15 * 60 + 30
INTERVAL_MINUTES = {"minute": 1, "1minute": 1, "3minute": 3, "5minute": 5,
                    "10minute": 10, "15minute": 15, "30minute": 30, "60minute": 60}


def stored_days(store, symbol: str, interval: str) -> tuple[set[date], set[date]]:
    """IST session dates stored for a symbol: (complete, any)."""
    try:
        frame = store.read_candles(symbol, interval, columns=["timestamp"])
    except Exception:  # unreadable data counts as missing
        return set(), set()
    if frame.empty:
        return set(), set()
    stamp = pd.to_datetime(frame["timestamp"], utc=True).dt.tz_convert(IST)
    span = pd.DataFrame({"day": stamp.dt.date, "minute": stamp.dt.hour * 60 + stamp.dt.minute}
                        ).groupby("day")["minute"].agg(["min", "max"])
    bar = INTERVAL_MINUTES.get(interval, 1)
    complete = span[(span["min"] <= SESSION_OPEN_MINUTE) & (span["max"] >= SESSION_EXIT_MINUTE - bar)]
    return set(complete.index), set(span.index)


def manifest_path(symbol: str, interval: str, root: Path | None = None) -> Path:
    if root is None:
        from trading_agent.core.config import get_settings

        root = get_settings().data_dir
    return Path(root) / "ingest_coverage" / interval / f"{symbol}.json"


def _ranges(path: Path) -> list[list[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))["fetched"]
    except (OSError, ValueError, KeyError):
        return []


def record_fetch(symbol: str, interval: str, start: datetime, end: datetime, root: Path | None = None) -> None:
    """Remember that Kite returned everything it had for ``[start, end]`` (merged with earlier windows)."""
    path = manifest_path(symbol, interval, root)
    spans = sorted([(pd.Timestamp(a), pd.Timestamp(b)) for a, b in _ranges(path)]
                   + [(pd.Timestamp(start).tz_convert("UTC"), pd.Timestamp(end).tz_convert("UTC"))])
    merged = [list(spans[0])]
    for a, b in spans[1:]:
        if a <= merged[-1][1] + pd.Timedelta(minutes=1):
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps({"fetched": [[a.isoformat(), b.isoformat()] for a, b in merged]}), encoding="utf-8")
    os.replace(tmp, path)


def fetched_sessions(symbol: str, interval: str, days, root: Path | None = None) -> set[date]:
    """Sessions among ``days`` whose whole 09:15-15:30 IST window lies inside a recorded fetch."""
    spans = [(pd.Timestamp(a), pd.Timestamp(b)) for a, b in _ranges(manifest_path(symbol, interval, root))]
    if not spans:
        return set()
    covered = set()
    for day in days:
        open_ = pd.Timestamp(f"{day} 09:15", tz=IST)
        close = pd.Timestamp(f"{day} 15:30", tz=IST)
        if any(a <= open_ and b >= close for a, b in spans):
            covered.add(day)
    return covered


def covered_days(store, symbol: str, interval: str, root: Path | None = None) -> tuple[set[date], set[date]]:
    """(covered, any): complete sessions plus sessions fully fetched from Kite, and every session with bars."""
    complete, seen = stored_days(store, symbol, interval)
    return complete | fetched_sessions(symbol, interval, seen, root), seen


def last_closed_session(now: datetime | None = None) -> date:
    """The latest weekday whose 15:30 IST close has passed (holidays are not known)."""
    stamp = pd.Timestamp(now or datetime.now(UTC)).tz_convert(IST)
    day = stamp.date()
    if stamp.hour * 60 + stamp.minute < SESSION_CLOSE_MINUTE:
        day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def window_start(days: int, now: datetime | None = None) -> date:
    """First session date of a ``days``-long window ending at the last closed session.

    ``check`` and ``backfill --missing-only`` share it, so a top-up covers exactly what is checked.
    """
    return last_closed_session(now) - timedelta(days=days)


def check(symbols: list[str], interval: str = "minute", days: int = 30, store=None,
          now: datetime | None = None, manifest_root: Path | None = None) -> dict:
    """Per-symbol coverage over the last ``days`` calendar days, plus store-wide staleness."""
    from trading_agent.storage.parquet_store import get_parquet_store

    store = store or get_parquet_store()
    stored = {symbol: stored_days(store, symbol, interval) for symbol in symbols}
    calendar = sorted(set().union(*(seen for _, seen in stored.values())) if stored else set())
    expected = last_closed_session(now)
    first = window_start(days, now)
    required = [d for d in calendar if first <= d <= expected]
    # A session a thin stock never traded in has no bars; a recorded full fetch still covers it.
    stored = {symbol: (complete | fetched_sessions(symbol, interval, required, manifest_root), seen)
              for symbol, (complete, seen) in stored.items()}
    latest = required[-1] if required else None
    rows, counts = {}, {"up_to_date": 0, "behind": 0, "holes": 0, "empty": 0}
    for symbol, (complete, seen) in stored.items():
        if not seen:
            status, last, missing = "empty", None, []
        else:
            done = [d for d in required if d in complete]
            last = done[-1] if done else None
            missing = [d for d in required if d not in complete and (last is None or d < last)]
            status = "behind" if last is None or last < latest else "holes" if missing else "up_to_date"
        counts[status] += 1
        if status != "up_to_date":
            rows[symbol] = {"status": status, "last_complete": str(last) if last else None,
                            "missing": [str(d) for d in missing]}
    return {
        "interval": interval, "window_days": days, "symbols": len(symbols),
        "latest_stored_session": str(calendar[-1]) if calendar else None,
        "latest_required_session": str(latest) if latest else None,
        "last_closed_weekday": str(expected),
        "store_current": bool(calendar) and calendar[-1] >= expected,
        "sessions_in_window": len(required), "counts": counts, "problems": rows,
        "ok": bool(calendar) and calendar[-1] >= expected and counts["up_to_date"] == len(symbols),
    }
