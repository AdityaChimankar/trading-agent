"""Symbols router."""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from trading_agent.ingest.instrument_lookup import get_instrument_lookup
from trading_agent.storage.parquet_store import get_parquet_store

router = APIRouter()


class SymbolInfo(BaseModel):
    symbol: str
    name: str | None = None
    exchange: str = "NSE"
    instrument_token: int | None = None
    lot_size: int = 1
    sector: str | None = None
    has_data: bool = False
    data_range: dict | None = None


@router.get("/stored", response_model=list[SymbolInfo])
def list_stored_symbols():
    """Offline catalogue for the dashboard; never authenticates with a broker."""
    return [SymbolInfo(symbol=symbol, has_data=True) for symbol in sorted(get_parquet_store().get_symbols())]


@router.get("/", response_model=list[SymbolInfo])
async def list_symbols(
    search: str | None = Query(None, description="Search query"),
    limit: int = Query(100, ge=1, le=500),
    has_data: bool | None = Query(None, description="Filter by data availability"),
):
    """List available symbols."""
    parquet = get_parquet_store()
    instruments = get_instrument_lookup()

    parquet_symbols = set(parquet.get_symbols())
    try:
        all_symbols = instruments.get_all_equity_symbols()
    except Exception:
        # A fresh dashboard must still start without Kite credentials/network.
        all_symbols = sorted(parquet_symbols)

    if search:
        search = search.upper()
        all_symbols = [s for s in all_symbols if search in s]

    if has_data is not None:
        if has_data:
            all_symbols = [s for s in all_symbols if s in parquet_symbols]
        else:
            all_symbols = [s for s in all_symbols if s not in parquet_symbols]

    symbols = all_symbols[:limit]

    result = []
    for symbol in symbols:
        try:
            token = instruments.get_token(symbol)
            lot_size = instruments.get_lot_size(symbol)
        except Exception:
            token = None
            lot_size = 1
        sym_has_data = symbol in parquet_symbols

        data_range = None
        if sym_has_data:
            start, end = parquet.get_date_range(symbol)
            if start and end:
                data_range = {"start": start.isoformat(), "end": end.isoformat()}

        result.append(
            SymbolInfo(
                symbol=symbol,
                instrument_token=token,
                lot_size=lot_size,
                has_data=sym_has_data,
                data_range=data_range,
            )
        )

    return result


@router.get("/{symbol}", response_model=SymbolInfo)
async def get_symbol(symbol: str):
    """Get symbol details."""
    symbol = symbol.upper()
    parquet = get_parquet_store()
    instruments = get_instrument_lookup()

    has_data = symbol in parquet.get_symbols()
    try:
        token = instruments.get_token(symbol)
        lot_size = instruments.get_lot_size(symbol)
    except Exception:
        token = None
        lot_size = 1
    if not token and not has_data:
        raise HTTPException(status_code=404, detail="Symbol not found")

    data_range = None
    if has_data:
        start, end = parquet.get_date_range(symbol)
        if start and end:
            data_range = {"start": start.isoformat(), "end": end.isoformat()}

    return SymbolInfo(
        symbol=symbol,
        instrument_token=token,
        lot_size=lot_size,
        has_data=has_data,
        data_range=data_range,
    )


@router.get("/{symbol}/intervals")
async def get_symbol_intervals(symbol: str):
    """Get available intervals for a symbol."""
    symbol = symbol.upper()
    parquet = get_parquet_store()
    intervals = parquet.get_intervals(symbol)
    return {"symbol": symbol, "intervals": intervals}


@router.get("/{symbol}/technicals")
def get_symbol_technicals(symbol: str, interval: str = Query("5minute")):
    """Groww-style Bullish/Neutral/Bearish indicator vote (comparison view only)."""
    from trading_agent.analysis.technical_summary import get_technical_summary

    summary = get_technical_summary(symbol.upper(), interval)
    if summary is None:
        raise HTTPException(status_code=404, detail="Not enough candles for a technical summary")
    return summary
