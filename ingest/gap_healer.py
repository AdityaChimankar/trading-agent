"""
Detect holes in the stored candle stream and repair them from Kite's
historical endpoint.

The live ticker can only write candles for minutes it was actually
listening. A dropped WebSocket, an expired token, a laptop that slept, or a
process restart all produce the same symptom: minutes of data that never
arrive, and no indication that they are missing. The candle table cannot
distinguish "nobody traded" from "we weren't watching" - this module can,
because it knows what the session timeline *should* look like and compares
the stored minutes against it.

Repair is a historical refetch over the exact hole, which is cheap because
only symbols that are genuinely behind cost a request. That matters: a naive
"refetch everything on reconnect" sweep at 500 symbols is 500 requests at
Kite's ~3/sec historical limit, i.e. minutes of work, and it would burn the
rate limit every time the socket hiccups.

Kite returns only minutes in which a trade happened, so a healed gap that
recovers 0 rows is a real answer ("the symbol genuinely didn't trade"), not a
failure - the gap row records it as healed either way.

Standalone use (a manual sweep, or after a crash):

    python -m ingest.gap_healer                 # repair today, whole watchlist
    python -m ingest.gap_healer --recent 90     # just the last 90 minutes
"""
import sys
import time
from datetime import datetime, timedelta

from core.freshness import (MARKET_CLOSE, MARKET_OPEN, MINUTE_FMT, MINUTE_KEY_FMT,
                           candle_timestamp, minute_key, session_end, session_start)
from storage.db import get_connection, get_watchlist

# Same throttle the backfill uses: ~2.5 req/sec, under Kite's ~3/sec cap.
REQUEST_DELAY_SEC = 0.4
# Re-exported so ingest.live_ticker can import the minute format from here.
__all__ = ["MINUTE_FMT", "heal_window", "heal_day", "heal_recent", "last_completed_minute",
           "record_gap", "heal_symbol", "missing_minutes", "session_minutes", "pending_gaps"]


def session_minutes(start: datetime, end: datetime) -> list:
    """Every minute timestamp the exchange should have produced between
    `start` and `end` inclusive, i.e. weekdays inside 9:15-15:30."""
    out = []
    cursor = start.replace(second=0, microsecond=0)
    while cursor <= end:
        if cursor.weekday() < 5 and MARKET_OPEN <= cursor.time() <= MARKET_CLOSE:
            out.append(cursor)
        cursor += timedelta(minutes=1)
    return out


def _window_bounds(start: datetime, end: datetime) -> tuple:
    """Half-open (start, end_exclusive) bounds for a minute window.

    Both bounds are 16-char minute strings, and every stored form of a minute
    - naive "...T09:15:00" or Kite's "...T09:15:00+05:30" - sorts inside
    them, because the 16-char prefix decides the order and the longer form
    only differs after it. That makes the range comparison format-agnostic
    while still using the (symbol, timestamp) index, which a substr()
    predicate would not.
    """
    return (start.strftime(MINUTE_KEY_FMT),
            (end + timedelta(minutes=1)).strftime(MINUTE_KEY_FMT))


def missing_minutes(conn, symbol: str, start: datetime, end: datetime) -> list:
    """Minutes the session should contain but the DB doesn't, for one symbol.

    Reads the stored minutes in the window (at most a few hundred indexed
    rows) and diffs them against the session timeline. An empty result is the
    common case and costs one small query, which is what keeps a
    heal-everything sweep affordable.

    Compares minute PREFIXES rather than raw timestamps: the historical
    backfill stored Kite's "+05:30" offset while the live ticker writes naive
    timestamps, so an exact string match would report every backfilled minute
    as missing and refetch all 500 symbols on every sweep.
    """
    expected = session_minutes(start, end)
    if not expected:
        return []
    lower, upper = _window_bounds(start, end)
    rows = conn.execute(
        "SELECT substr(timestamp, 1, 16) AS minute FROM candles "
        "WHERE symbol = ? AND timestamp >= ? AND timestamp < ?",
        (symbol, lower, upper),
    ).fetchall()
    stored = {r["minute"] for r in rows}
    return [m for m in expected if minute_key(m) not in stored]


def _fetch_minute_candles(kite, instrument_token, start: datetime, end: datetime, max_retries: int = 3):
    """One historical request, retried on rate-limit responses.

    Deliberately local rather than importing fetch_historical's helper: that
    module resolves the whole watchlist at import time and exits the process
    if watchlist_resolved.py is missing. Pulling that into the live ticker's
    import graph would let a missing watchlist file kill the live feed.
    """
    for attempt in range(max_retries):
        try:
            return kite.historical_data(
                instrument_token=instrument_token, from_date=start, to_date=end,
                interval="minute",
            )
        except Exception as e:
            text = str(e)
            if "Too many requests" in text or "429" in text:
                wait = 2 ** attempt * 2
                print(f"  rate limited, backing off {wait}s...")
                time.sleep(wait)
            else:
                raise
    return []


def record_gap(conn, symbol: str, start: datetime, end: datetime, minutes: int) -> int:
    """Log a detected hole. Returns its id so the heal can close the same row."""
    cur = conn.execute(
        "INSERT INTO candle_gaps (symbol, gap_start, gap_end, minutes, detected_at, status) "
        "VALUES (?, ?, ?, ?, ?, 'pending')",
        (symbol, start.strftime(MINUTE_FMT), end.strftime(MINUTE_FMT), minutes,
         datetime.now().isoformat()),
    )
    conn.commit()
    return cur.lastrowid


def heal_symbol(kite, conn, symbol: str, instrument_token, start: datetime,
                end: datetime, gap_id: int | None = None, delay: float = REQUEST_DELAY_SEC) -> int:
    """Refetch one symbol's window and upsert whatever comes back. Returns the
    number of candle rows written."""
    try:
        candles = _fetch_minute_candles(kite, instrument_token, start, end)
    except Exception as e:
        if gap_id is not None:
            conn.execute("UPDATE candle_gaps SET status = 'failed' WHERE id = ?", (gap_id,))
            conn.commit()
        print(f"  {symbol}: heal failed ({e})")
        return 0

    rows = [
        (symbol, candle_timestamp(c["date"]), c["open"], c["high"], c["low"], c["close"], c["volume"])
        for c in candles
        if c.get("date") is not None
    ]
    # Replace, don't accumulate: any row already covering this window is
    # deleted first, in the same transaction as the insert. INSERT OR REPLACE
    # alone is not enough here - it keys on the exact timestamp string, so a
    # legacy "+05:30" row for the same minute would survive alongside the
    # naive replacement and double-count that bar.
    lower, upper = _window_bounds(start, end)
    conn.execute(
        "DELETE FROM candles WHERE symbol = ? AND timestamp >= ? AND timestamp < ?",
        (symbol, lower, upper),
    )
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO candles (symbol, timestamp, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", rows,
        )
    conn.commit()

    if gap_id is not None:
        # 0 rows is a legitimate outcome: the symbol had no trades in that
        # window. The hole was ours to explain, and this is the explanation.
        conn.execute(
            "UPDATE candle_gaps SET status = 'healed', healed_at = ?, healed_candles = ? WHERE id = ?",
            (datetime.now().isoformat(), len(rows), gap_id),
        )
        conn.commit()

    if delay:
        time.sleep(delay)  # stay under Kite's historical rate limit
    return len(rows)


def heal_window(kite, conn, start: datetime, end: datetime, symbols=None,
                delay: float = REQUEST_DELAY_SEC) -> dict:
    """Repair every hole in [start, end] across `symbols` (default: watchlist).

    Returns a summary dict. Never raises for a single symbol's failure - one
    bad symbol must not abort a sweep that other symbols still need.
    """
    symbols = symbols if symbols is not None else get_watchlist()
    healed, clean, failed, candles = 0, 0, 0, 0

    for w in symbols:
        symbol, token = w["symbol"], w["instrument_token"]
        try:
            missing = missing_minutes(conn, symbol, start, end)
        except Exception as e:
            print(f"  {symbol}: gap scan failed ({e})")
            failed += 1
            continue

        if not missing:
            clean += 1
            continue

        gap_id = record_gap(conn, symbol, missing[0], missing[-1], len(missing))
        written = heal_symbol(kite, conn, symbol, token, missing[0], end, gap_id=gap_id, delay=delay)
        # The gap row is the source of truth about whether the repair worked:
        # 0 recovered candles is still a success when the symbol simply had no
        # trades in that window.
        if _gap_status(conn, gap_id) == "healed":
            healed += 1
        else:
            failed += 1
        candles += written

    return {
        "window": f"{start.strftime(MINUTE_FMT)} .. {end.strftime(MINUTE_FMT)}",
        "symbols_checked": len(symbols),
        "symbols_already_complete": clean,
        "symbols_healed": healed,
        "symbols_failed": failed,
        "candles_recovered": candles,
    }


def _gap_status(conn, gap_id: int) -> str:
    row = conn.execute("SELECT status FROM candle_gaps WHERE id = ?", (gap_id,)).fetchone()
    return row["status"] if row else "failed"


def last_completed_minute(now: datetime | None = None) -> datetime:
    """The newest minute that can legitimately be closed already. Healing
    always stops here: the in-progress minute is still being built by the
    ticker, so refetching it would write a partial bar over a live one."""
    now = now or datetime.now()
    return (now - timedelta(minutes=1)).replace(second=0, microsecond=0)


def heal_day(kite=None, conn=None, now: datetime | None = None, symbols=None,
             delay: float = REQUEST_DELAY_SEC) -> dict:
    """Sweep the whole session so far. This is the definitive pass: run it
    after the close, or after any crash/restart, and every minute the
    pipeline was blind gets filled from the exchange's own record."""
    conn = conn or get_connection()
    if kite is None:
        from ingest.kite_auth import get_kite_client
        kite = get_kite_client()
    now = now or datetime.now()
    start = session_start(now)
    end = min(last_completed_minute(now), session_end(now))
    if end < start:
        return {"window": None, "note": "session has not produced a completed minute yet",
                "symbols_checked": 0, "symbols_already_complete": 0,
                "symbols_healed": 0, "symbols_failed": 0, "candles_recovered": 0}
    return heal_window(kite, conn, start, end, symbols=symbols, delay=delay)


def heal_recent(minutes: int, kite=None, conn=None, symbols=None,
                delay: float = REQUEST_DELAY_SEC) -> dict:
    """Repair just the last `minutes` - the targeted sweep after a reconnect,
    where the outage window is already known."""
    conn = conn or get_connection()
    if kite is None:
        from ingest.kite_auth import get_kite_client
        kite = get_kite_client()
    now = datetime.now()
    start = max(session_start(now), (now - timedelta(minutes=minutes)).replace(second=0, microsecond=0))
    return heal_window(kite, conn, start, last_completed_minute(now), symbols=symbols, delay=delay)


def pending_gaps(conn=None) -> list:
    """Unhealed gaps, newest first - what the liveness surface reports."""
    conn = conn or get_connection()
    rows = conn.execute(
        "SELECT symbol, gap_start, gap_end, minutes, detected_at FROM candle_gaps "
        "WHERE status = 'pending' ORDER BY gap_start DESC LIMIT 50"
    ).fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    from ingest.kite_auth import get_kite_client

    args = sys.argv[1:]
    recent = None
    if "--recent" in args:
        idx = args.index("--recent")
        recent = int(args[idx + 1]) if len(args) > idx + 1 else 60

    kite = get_kite_client()
    conn = get_connection()
    if recent is not None:
        print(f"Healing the last {recent} minute(s)...")
        print(heal_recent(recent, kite=kite, conn=conn))
    else:
        print("Healing today's session...")
        print(heal_day(kite, conn))
    conn.close()
