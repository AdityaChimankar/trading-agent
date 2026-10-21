"""
Market-session awareness and candle freshness.

Why this exists: every read of the candles table takes the newest row it can
find. That is the correct thing to do, and it is also completely silent - if
the live feed dies at 11:40, every indicator, decision and P&L mark keeps
producing confident output from the 11:40 price for the rest of the session.
A stalled feed and a quiet market are indistinguishable from the data alone,
so freshness has to be judged against the wall clock *and* the trading
session. That is all this module does.

Timestamps are naive local time, matching the rest of the project (candles
are keyed by Kite's exchange timestamp, signals by datetime.now()). The
session constants only mean anything if the host clock is IST, which the
pipeline already assumes everywhere else.

Batch functions take a whole symbol list and do one grouped query on one
connection. That matters at 500 symbols: the per-symbol helpers opening their
own connection turn every decision cycle and every dashboard poll into 500
connect/close cycles.
"""
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from storage.db import get_connection

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

# The candle table's timestamp format: naive IST, minute precision. Kite
# returns tz-aware (+05:30) datetimes and the live ticker writes naive ones
# via strftime, so the SAME minute could be stored under two different
# strings - which breaks INSERT OR REPLACE (it duplicates instead of
# replacing), breaks equality checks in gap detection, and lets a min/max
# comparison pick either form. Everything that writes a candle goes through
# candle_timestamp() so a minute has exactly one identity.
MINUTE_FMT = "%Y-%m-%dT%H:%M:00"
MINUTE_KEY_FMT = "%Y-%m-%dT%H:%M"  # 16-char prefix: what both forms share
IST = ZoneInfo("Asia/Kolkata")


def candle_timestamp(value) -> str:
    """A candle timestamp in the project's storage format (naive IST).

    Accepts Kite's tz-aware datetimes, naive ones, and already-formatted
    strings, and returns the same string for the same instant either way.
    """
    if isinstance(value, str):
        return value[:16] + ":00"
    if value.tzinfo is not None:
        value = value.astimezone(IST).replace(tzinfo=None)
    return value.strftime(MINUTE_FMT)


def minute_key(value) -> str:
    """The 16-char minute identity both timestamp forms share, for comparing
    a stored row against a session minute without caring which form it is."""
    return candle_timestamp(value)[:16]

# A candle is "late" once the newest one is older than this. Two scheduler
# cycles, so one slow cycle doesn't raise a false alarm.
DEFAULT_MAX_AGE_MINUTES = 10

# The live ticker's flush thread writes a heartbeat every 5s. A dozen missed
# beats means the process is gone, not busy.
HEARTBEAT_MAX_AGE_SEC = 60


def in_market_hours(now: datetime | None = None) -> bool:
    """True inside the NSE cash session on a weekday."""
    now = now or datetime.now()
    if now.weekday() >= 5:  # Saturday / Sunday
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def session_start(now: datetime | None = None) -> datetime:
    """Start of the trading session containing `now`."""
    now = now or datetime.now()
    return datetime.combine(now.date(), MARKET_OPEN)


def session_end(now: datetime | None = None) -> datetime:
    """End of the trading session containing `now`."""
    now = now or datetime.now()
    return datetime.combine(now.date(), MARKET_CLOSE)


def _age_minutes(timestamp: str | None, now: datetime) -> float | None:
    if not timestamp:
        return None
    try:
        return (now - datetime.fromisoformat(timestamp)).total_seconds() / 60.0
    except (TypeError, ValueError):
        return None


def latest_candle_timestamp(symbol: str) -> str | None:
    """Newest stored candle timestamp for one symbol, or None if it has none."""
    conn = get_connection()
    row = conn.execute(
        "SELECT MAX(timestamp) AS ts FROM candles WHERE symbol = ?", (symbol,)
    ).fetchone()
    conn.close()
    return row["ts"] if row and row["ts"] else None


def latest_candle_timestamps(symbols) -> dict:
    """Newest candle per symbol for a whole watchlist, in one query on one
    connection. Symbols with no candles at all are simply absent."""
    symbols = list(symbols)
    if not symbols:
        return {}
    conn = get_connection()
    try:
        placeholders = ",".join("?" * len(symbols))
        rows = conn.execute(
            f"SELECT symbol, MAX(timestamp) AS ts FROM candles "  # noqa: S608 - placeholders only
            f"WHERE symbol IN ({placeholders}) GROUP BY symbol",
            tuple(symbols),
        ).fetchall()
    finally:
        conn.close()
    return {r["symbol"]: r["ts"] for r in rows}


def candle_age_minutes(symbol: str, now: datetime | None = None) -> float | None:
    """Minutes since the symbol's newest candle, or None if it has no candles."""
    return _age_minutes(latest_candle_timestamp(symbol), now or datetime.now())


def is_stale(symbol: str, max_age_minutes: float = DEFAULT_MAX_AGE_MINUTES,
             now: datetime | None = None) -> bool:
    """True when acting on this symbol's newest candle would mean acting on a
    price that no longer exists.

    Outside the trading session this is always False: old data is then the
    correct state of the world, not a failure, and flagging it would make
    every after-hours and overnight run scream. A symbol with no candles at
    all is also not "stale" - that's a "not backfilled yet" problem, which the
    pre-flight check reports, not this.
    """
    now = now or datetime.now()
    if not in_market_hours(now):
        return False
    age = candle_age_minutes(symbol, now=now)
    return age is not None and age > max_age_minutes


def split_stale(symbols, max_age_minutes: float = DEFAULT_MAX_AGE_MINUTES) -> tuple:
    """Split `symbols` into (fresh, stale) - the shape every decision cycle
    needs, asked the same way everywhere, from a single clock reading and a
    single grouped query."""
    symbols = list(symbols)
    now = datetime.now()
    if not in_market_hours(now):
        return symbols, []  # nothing is stale when nothing is trading
    todos = latest_candle_timestamps(symbols)
    fresh, stale = [], []
    for symbol in symbols:
        age = _age_minutes(todos.get(symbol), now)
        if age is not None and age > max_age_minutes:
            stale.append(symbol)
        else:
            fresh.append(symbol)
    return fresh, stale


def stale_symbols(symbols, max_age_minutes: float = DEFAULT_MAX_AGE_MINUTES) -> list:
    """Just the stale half of split_stale()."""
    return split_stale(symbols, max_age_minutes=max_age_minutes)[1]


def warn_stale(stale, context: str) -> None:
    """One loud line whenever a cycle drops symbols, because the failure this
    replaces was total silence: the newest candle simply gets older while the
    indicators keep looking perfectly current. Every decision path reports a
    skip the same way, so a dead feed can't be mistaken for a quiet market."""
    if not stale:
        return
    shown = ", ".join(list(stale)[:5]) + (" ..." if len(stale) > 5 else "")
    print(f"[stale] {context}: skipping {len(stale)} symbol(s) whose candles are "
          f"behind the live feed: {shown}")


def ingest_heartbeat() -> dict | None:
    """The live ticker's heartbeat row, with a computed age and an `alive`
    verdict. None means no ticker has ever run against this database.

    This is what makes "the feed is down" a readable fact rather than an
    inference: candles alone can't distinguish a dead feed from a quiet
    market, but a heartbeat that stopped can't be anything else.
    """
    conn = get_connection()
    row = conn.execute("SELECT * FROM ingest_status WHERE id = 1").fetchone()
    conn.close()
    if row is None:
        return None
    status = dict(row)
    age = None
    if status.get("updated_at"):
        try:
            age = (datetime.now() - datetime.fromisoformat(status["updated_at"])).total_seconds()
        except ValueError:
            age = None
    status["heartbeat_age_sec"] = round(age, 1) if age is not None else None
    status["alive"] = bool(
        age is not None and age <= HEARTBEAT_MAX_AGE_SEC and status.get("connected")
    )
    return status


def feed_summary(symbols, max_age_minutes: float = DEFAULT_MAX_AGE_MINUTES) -> dict:
    """One dict describing whether the data behind `symbols` is usable right
    now, combining the candles with the ticker's heartbeat. Either signal
    alone misleads: a connected socket can deliver nothing, and candles that
    look fresh at a glance can be an hour old."""
    symbols = list(symbols)
    now = datetime.now()
    open_now = in_market_hours(now)
    todos = latest_candle_timestamps(symbols)
    ages = [(s, _age_minutes(todos.get(s), now)) for s in symbols]
    known = [(s, a) for s, a in ages if a is not None]
    stale = [s for s, a in known if a > max_age_minutes]
    freshest = min((a for _, a in known), default=None)
    heartbeat = ingest_heartbeat()

    summary = {
        "market_open": open_now,
        "checked_at": now.isoformat(),
        "session": f"{session_start(now).isoformat()} .. {session_end(now).isoformat()}",
        "symbols": len(symbols),
        "symbols_with_data": len(known),
        # Only a failure while the session is running; afterwards stale is normal.
        "stale_symbols": len(stale) if open_now else 0,
        "stale_examples": stale[:10] if open_now else [],
        "newest_candle_age_minutes": round(freshest, 1) if freshest is not None else None,
        "max_age_minutes": max_age_minutes,
        "ticker_alive": bool(heartbeat and heartbeat["alive"]),
        "ticker_heartbeat_age_sec": heartbeat["heartbeat_age_sec"] if heartbeat else None,
    }
    summary["status"] = _status(summary, heartbeat)
    return summary


def _status(summary: dict, heartbeat: dict | None) -> str:
    """One word for the UI: is the pipeline delivering?

    - closed:   outside the session, nobody is expected to be delivering
    - live:     heartbeat current and every symbol's candles are fresh
    - degraded: heartbeat current, but the data has fallen behind anyway
    - down:     no heartbeat, or the socket is gone, during the session
    """
    if not summary["market_open"]:
        return "closed"
    if not heartbeat or not heartbeat["alive"]:
        return "down"
    if summary["stale_symbols"]:
        return "degraded"
    return "live"


if __name__ == "__main__":
    # Quick manual read: what does the feed look like from here?
    from storage.db import get_watchlist_symbols

    syms = get_watchlist_symbols()
    if not syms:
        raise SystemExit("Watchlist is empty - run `python -m ingest.fetch_historical` first.")
    summary = feed_summary(syms)
    for key, value in summary.items():
        print(f"{key}: {value}")

    print("--- live ticker ---")
    heartbeat = ingest_heartbeat()
    if heartbeat is None:
        print("no heartbeat recorded - ingest.live_ticker has not run against this DB")
    else:
        for key in ("alive", "connected", "last_tick_at", "heartbeat_age_sec", "symbols_seen",
                    "buckets_pending", "candles_written", "heal_queue_depth", "reconnects"):
            print(f"{key}: {heartbeat.get(key)}")

    if summary["status"] == "down":
        print("\nWARNING: the live feed is down during market hours. "
              "Restart `python -m ingest.live_ticker`, then run "
              "`python -m ingest.gap_healer` to fill the hole.")
    elif summary["status"] == "degraded":
        print(f"\nWARNING: {summary['stale_symbols']} symbol(s) are behind the live feed.")
