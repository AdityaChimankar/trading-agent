"""End-of-day digest endpoint."""
from datetime import date as _date

from fastapi import APIRouter, Query

from api.serializers import jsonable
from db import get_connection

router = APIRouter(prefix="/api", tags=["digest"])


@router.get("/digest")
def digest(date: str | None = Query(None, description="ISO date, defaults to today")):
    """The stored narrative summary for a trading day, if one has been
    generated (scheduler.py runs digest.generate_digest() at 3:35 PM)."""
    target = date or _date.today().isoformat()
    conn = get_connection()
    row = conn.execute(
        "SELECT summary, generated_at FROM digests WHERE date = ?", (target,)
    ).fetchone()
    conn.close()

    if row is None:
        return {"date": target, "summary": None, "generated_at": None}
    return jsonable({"date": target, "summary": row["summary"], "generated_at": row["generated_at"]})
