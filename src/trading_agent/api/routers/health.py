"""Health check router."""

from datetime import UTC

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    version: str
    timestamp: str


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint."""
    from datetime import datetime

    from trading_agent import __version__

    return HealthResponse(
        status="healthy",
        version=__version__,
        timestamp=datetime.now(UTC).isoformat(),
    )


@router.get("/ready")
async def readiness_check():
    """Readiness check endpoint."""
    from trading_agent.storage.db import Base, get_database

    try:
        db = get_database()
        async with db.session() as session:
            # Resolve every mapped column without reading account data. A DB
            # connection alone is not sufficient when an upgrade is missing.
            for table in Base.metadata.sorted_tables:
                await session.execute(select(table).limit(0))
        return {"status": "ready"}
    except Exception:
        return JSONResponse(status_code=503, content={
            "status": "not ready", "error": "Database unavailable or schema upgrade required",
        })
