"""SQLite connection + one-time schema setup."""
import sqlite3
from paths import DB_PATH, SCHEMA_PATH


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    
    # Execute as a single script for faster initialization
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;      -- Fast disk writes (safe in WAL mode)
        PRAGMA temp_store=MEMORY;       -- Store temp tables/indices in RAM
        PRAGMA mmap_size=30000000000;   -- Use memory mapping for ultra-fast reads
        PRAGMA busy_timeout=30000;
    """)
    conn.row_factory = sqlite3.Row
    return conn
def get_open_position_symbols() -> set:
    """Symbols with a currently-open paper position, for cross-referencing
    against the watchlist ranking (so the sidebar can flag them)."""
    conn = get_connection()
    rows = conn.execute("SELECT DISTINCT symbol FROM open_positions WHERE status = 'open'").fetchall()
    conn.close()
    return {r["symbol"] for r in rows}

def init_db() -> None:
    conn = get_connection()
    with open(SCHEMA_PATH) as f:
        conn.executescript(f.read())
    conn.commit()
    _migrate_add_missing_columns(conn)
    conn.close()
    print(f"DB ready at {DB_PATH}")


def _migrate_add_missing_columns(conn: sqlite3.Connection) -> None:
    """CREATE TABLE IF NOT EXISTS is a no-op on a table that already
    exists, so a schema.sql change that adds a column to an existing
    table won't apply to a DB someone already created. This adds any
    genuinely new columns safely, ignoring 'duplicate column' errors
    for columns that are already there. Add new (table, column, type)
    entries here whenever schema.sql adds a column to an EXISTING
    table (new tables don't need this - CREATE TABLE IF NOT EXISTS
    handles those fine on its own)."""
    migrations = [
        ("open_positions", "stop_loss", "REAL"),
        ("open_positions", "take_profit", "REAL"),
    ]
    for table, column, col_type in migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            conn.commit()
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise  # a real error, not just "already migrated" - don't swallow it


def record_realized_cost(
    symbol: str,
    signal_timestamp: str,
    signal_price: float,
    signal_action: str,
    fill_timestamp: str,
    fill_price: float,
    atr_at_signal: float = None,
    session_minute: int = None,
    volume_at_fill: int = None,
) -> None:
    """Record the realized execution cost for a signal-to-fill transition.
    
    This captures the actual slippage (difference between signal price and fill price),
    enabling cost-aware position sizing and symbol selection.
    """
    from datetime import datetime
    
    slippage_pct = ((fill_price - signal_price) / signal_price) * 100
    if signal_action == "SELL":
        slippage_pct = -slippage_pct  # normalize: positive = cost for both BUY and SELL
    
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO realized_costs 
               (symbol, signal_timestamp, signal_price, signal_action, 
                fill_timestamp, fill_price, slippage_pct, atr_at_signal,
                session_minute, volume_at_fill, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (symbol, signal_timestamp, signal_price, signal_action,
             fill_timestamp, fill_price, slippage_pct, atr_at_signal,
             session_minute, volume_at_fill, datetime.now().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def get_realized_costs(symbol: str = None, limit: int = 1000) -> list:
    """Get recent realized costs for analysis."""
    conn = get_connection()
    try:
        if symbol:
            rows = conn.execute(
                "SELECT * FROM realized_costs WHERE symbol = ? ORDER BY signal_timestamp DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM realized_costs ORDER BY signal_timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_realized_cost_stats(symbol: str = None, min_samples: int = 10) -> dict:
    """Get statistics on realized costs for cost model calibration."""
    import statistics
    
    costs = get_realized_costs(symbol, limit=5000)
    if not costs:
        return {"error": "no realized cost data"}
    
    if symbol:
        costs = [c for c in costs if c["symbol"] == symbol]
    
    if len(costs) < min_samples:
        return {"error": f"insufficient samples: {len(costs)} < {min_samples}"}
    
    slippages = [c["slippage_pct"] for c in costs]
    
    # Group by session minute for time-of-day analysis
    by_session = {}
    for c in costs:
        if c["session_minute"] is not None:
            bucket = (c["session_minute"] // 30) * 30  # 30-min buckets
            by_session.setdefault(bucket, []).append(c["slippage_pct"])
    
    session_stats = {}
    for bucket, vals in by_session.items():
        if len(vals) >= 5:
            hour = bucket // 60
            minute = bucket % 60
            session_stats[f"{hour:02d}:{minute:02d}"] = {
                "samples": len(vals),
                "median": round(statistics.median(vals), 4),
                "p95": round(sorted(vals)[int(len(vals) * 0.95)], 4),
            }
    
    return {
        "symbol": symbol or "ALL",
        "samples": len(slippages),
        "median_slippage_pct": round(statistics.median(slippages), 4),
        "mean_slippage_pct": round(statistics.mean(slippages), 4),
        "p50_slippage_pct": round(statistics.median(slippages), 4),
        "p95_slippage_pct": round(sorted(slippages)[int(len(slippages) * 0.95)], 4),
        "max_slippage_pct": round(max(slippages), 4),
        "min_slippage_pct": round(min(slippages), 4),
        "by_session": session_stats,
    }


def get_watchlist() -> list:
    """Single source of truth for the watchlist - reads from the DB
    (populated by ingest.fetch_historical's seed_watchlist(), which
    itself reads ingest/watchlist_resolved.py). Every file that needs symbols or
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
