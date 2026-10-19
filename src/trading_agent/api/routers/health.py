"""Health check router."""

from datetime import UTC

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

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
    # Check database, cache, etc.
    from trading_agent.storage.db import get_database

    try:
        db = get_database()
        async with db.session() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ready"}
    except Exception as e:
        return {"status": "not ready", "error": str(e)}
