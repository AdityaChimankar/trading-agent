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
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

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


def last_closed_session(now: datetime | None = None) -> date:
    """The latest weekday whose 15:30 IST close has passed (holidays are not known)."""
    stamp = pd.Timestamp(now or datetime.now(UTC)).tz_convert(IST)
    day = stamp.date()
    if stamp.hour * 60 + stamp.minute < SESSION_CLOSE_MINUTE:
        day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def check(symbols: list[str], interval: str = "minute", days: int = 30, store=None,
          now: datetime | None = None) -> dict:
    """Per-symbol coverage over the last ``days`` calendar days, plus store-wide staleness."""
    from trading_agent.storage.parquet_store import get_parquet_store

    store = store or get_parquet_store()
    stored = {symbol: stored_days(store, symbol, interval) for symbol in symbols}
    calendar = sorted(set().union(*(seen for _, seen in stored.values())) if stored else set())
    expected = last_closed_session(now)
    first = expected - timedelta(days=days)
    required = [d for d in calendar if first <= d <= expected]
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
