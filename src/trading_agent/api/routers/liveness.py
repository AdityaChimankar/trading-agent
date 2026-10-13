"""Liveness and data freshness router."""

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

from trading_agent.core.freshness import get_freshness_monitor, FreshnessStatus
from trading_agent.storage.parquet_store import get_parquet_store

router = APIRouter()


class FreshnessResponse(BaseModel):
    source: str
    status: str
    last_update: str | None
    age_seconds: float | None
    message: str


@router.get("/freshness", response_model=List[FreshnessResponse])
async def get_freshness():
    """Get data freshness for all sources."""
    monitor = get_freshness_monitor()
    summary = monitor.get_status_summary()

    results = []
    for source, details in summary["details"].items():
        results.append(
            FreshnessResponse(
                source=source,
                status=details["status"],
                last_update=details["last_update"],
                age_seconds=details["age_seconds"],
                message=details["message"],
            )
        )

    return results


@router.get("/freshness/summary")
async def get_freshness_summary():
    """Get freshness summary."""
    monitor = get_freshness_monitor()
    return monitor.get_status_summary()


@router.get("/storage/stats")
async def get_storage_stats():
    """Get storage statistics."""
    parquet = get_parquet_store()
    return parquet.get_stats()


@router.get("/storage/symbols/{symbol}")
async def get_symbol_storage(symbol: str):
    """Get storage details for a symbol."""
    symbol = symbol.upper()
    parquet = get_parquet_store()

    intervals = parquet.get_intervals(symbol)
    if not intervals:
        return {"symbol": symbol, "intervals": []}

    details = {}
    for interval in intervals:
        start, end = parquet.get_date_range(symbol, interval)
        df = parquet.read_latest(symbol, interval, n=1)

        details[interval] = {
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "latest_candle": df.iloc[-1].to_dict() if not df.empty else None,
        }

    return {"symbol": symbol, "intervals": details}
