"""Live-feed liveness endpoint.

The dashboard polls this so "live" is something it *knows* rather than
something it assumes. Before this existed, a dead ticker looked exactly like a
quiet market in the UI: no error, no new candles, no way to tell.
"""
from datetime import date as _date

from fastapi import APIRouter

from api.serializers import jsonable
from core.freshness import feed_summary, ingest_heartbeat
from ingest.gap_healer import pending_gaps
from storage.db import get_connection, get_watchlist_symbols

router = APIRouter(prefix="/api", tags=["liveness"])


@router.get("/liveness")
def liveness():
    """Is the pipeline delivering data right now, and has anything been lost?

    `status` is the one field a client needs:
      - live     heartbeat current, every candle fresh
      - degraded heartbeat current but the data has fallen behind
      - down     no heartbeat or no socket during the session
      - closed   outside 9:15-15:30, so nobody is expected to deliver
    """
    symbols = get_watchlist_symbols()
    todays_prefix = f"{_date.today().isoformat()}T00:00:00"

    conn = get_connection()
    try:
        healed = conn.execute(
            "SELECT COUNT(*) AS gaps, COALESCE(SUM(healed_candles), 0) AS candles "
            "FROM candle_gaps WHERE status = 'healed' AND gap_start >= ?", (todays_prefix,)
        ).fetchone()
        failed = conn.execute(
            "SELECT COUNT(*) AS n FROM candle_gaps WHERE status = 'failed'"
        ).fetchone()
        recent = conn.execute(
            "SELECT symbol, gap_start, gap_end, minutes, healed_candles, status FROM candle_gaps "
            "ORDER BY gap_start DESC LIMIT 20"
        ).fetchall()
        pending = pending_gaps(conn)
    finally:
        conn.close()

    summary = feed_summary(symbols)
    return jsonable({
        "status": summary["status"],
        "market_open": summary["market_open"],
        "checked_at": summary["checked_at"],
        "ticker": ingest_heartbeat(),
        "data": summary,
        "gaps": {
            "pending_count": len(pending),
            "pending": pending,
            "healed_today": healed["gaps"],
            "candles_recovered_today": healed["candles"],
            "failed_count": failed["n"],
            "recent": [dict(r) for r in recent],
        },
    })


@router.get("/liveness/gaps")
def gaps():
    """Every detected hole in the candle stream, newest first, so a repaired
    outage is auditable rather than disappearing into the data."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT symbol, gap_start, gap_end, minutes, detected_at, healed_at, "
            "healed_candles, status FROM candle_gaps ORDER BY gap_start DESC LIMIT 200"
        ).fetchall()
    finally:
        conn.close()
    return jsonable({"gaps": [dict(r) for r in rows]})
