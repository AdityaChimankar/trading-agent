"""Historical data router."""

from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
import pandas as pd

from trading_agent.storage.parquet_store import get_parquet_store

router = APIRouter()


class Candle(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    oi: int | None = None


class HistoryResponse(BaseModel):
    symbol: str
    interval: str
    candles: List[Candle]
    count: int


@router.get("/{symbol}", response_model=HistoryResponse)
def get_history(
    symbol: str,
    interval: str = Query("5minute", description="Candle interval"),
    start: datetime | None = Query(None, description="Start timestamp"),
    end: datetime | None = Query(None, description="End timestamp"),
    limit: int = Query(1000, ge=1, le=10000, description="Maximum candles to return"),
):
    """Get historical candle data."""
    symbol = symbol.upper()
    parquet = get_parquet_store()

    if symbol not in parquet.get_symbols():
        raise HTTPException(status_code=404, detail="No data for symbol")

    if interval not in parquet.get_intervals(symbol):
        raise HTTPException(status_code=404, detail=f"Interval {interval} not available")

    df = parquet.read_candles(symbol, interval, start=start, end=end)

    if df.empty:
        return HistoryResponse(symbol=symbol, interval=interval, candles=[], count=0)

    # Limit results
    if len(df) > limit:
        df = df.tail(limit)

    candles = [
        Candle(
            timestamp=row["timestamp"],
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            volume=int(row["volume"]),
            oi=int(row["oi"]) if pd.notna(row.get("oi")) else None,
        )
        for _, row in df.iterrows()
    ]

    return HistoryResponse(
        symbol=symbol,
        interval=interval,
        candles=candles,
        count=len(candles),
    )


@router.get("/{symbol}/latest")
def get_latest_candle(
    symbol: str,
    interval: str = Query("5minute"),
    n: int = Query(1, ge=1, le=100),
):
    """Get latest N candles."""
    symbol = symbol.upper()
    parquet = get_parquet_store()

    df = parquet.read_latest(symbol, interval, n=n)

    if df.empty:
        return {"symbol": symbol, "interval": interval, "candles": [], "count": 0}

    candles = [
        {
            "timestamp": row["timestamp"].isoformat(),
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": int(row["volume"]),
        }
        for _, row in df.iterrows()
    ]

    return {"symbol": symbol, "interval": interval, "candles": candles, "count": len(candles)}


@router.get("/{symbol}/range")
def get_date_range(symbol: str, interval: str = Query("5minute")):
    """Get available date range for symbol."""
    symbol = symbol.upper()
    parquet = get_parquet_store()

    start, end = parquet.get_date_range(symbol, interval)

    if not start or not end:
        raise HTTPException(status_code=404, detail="No data available")

    return {
        "symbol": symbol,
        "interval": interval,
        "start": start.isoformat(),
        "end": end.isoformat(),
    }
