"""SQLite connection + one-time schema setup."""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "trading_agent.db"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    # This DB is written to concurrently by live_ticker.py (background
    # flush thread), scheduler.py, and read by dashboard.py at the same
    # time during market hours. SQLite's default mode only allows one
    # writer with no wait, so overlapping writes fail immediately with
    # "database is locked" instead of queuing. WAL mode lets readers and
    # a writer coexist, and busy_timeout makes any remaining contention
    # wait (up to 30s) and retry instead of erroring out right away.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_connection()
    with open(SCHEMA_PATH) as f:
        conn.executescript(f.read())
    conn.commit()
    conn.close()
    print(f"DB ready at {DB_PATH}")


def get_watchlist() -> list:
    """Single source of truth for the watchlist - reads from the DB
    (populated by fetch_historical.py's seed_watchlist(), which itself
    reads watchlist_resolved.py). Every file that needs symbols or
    tokens should call this instead of hardcoding its own list - a
    hardcoded list silently drifts out of sync the moment you change
    your watchlist anywhere else."""
    conn = get_connection()
    rows = conn.execute("SELECT symbol, instrument_token, exchange FROM watchlist").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_watchlist_symbols() -> list:
    """Just the symbol strings, for files that only need names not tokens."""
    return [w["symbol"] for w in get_watchlist()]


if __name__ == "__main__":
    init_db()
