"""Watchlist router."""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from trading_agent.ingest.watchlist_resolved import WatchlistManager, resolve_watchlist
from trading_agent.storage.db import WatchlistRepository, get_database

router = APIRouter()


class WatchlistItem(BaseModel):
    symbol: str
    name: str | None = None
    exchange: str = "NSE"
    instrument_token: int | None = None
    lot_size: int = 1
    is_active: bool = True
    sector: str | None = None


class WatchlistCreate(BaseModel):
    symbol: str
    name: str | None = None
    exchange: str = "NSE"
    instrument_token: int | None = None
    lot_size: int = 1
    sector: str | None = None


@router.get("/", response_model=list[WatchlistItem])
async def list_watchlist(active_only: bool = Query(True)):
    """List watchlist symbols."""
    async with get_database().session() as session:
        repo = WatchlistRepository(session)

        if active_only:
            watchlist = await repo.get_active()
        else:
            watchlist = await repo.get_all(limit=1000)

        return [
            WatchlistItem(
                symbol=w.symbol,
                name=w.name,
                exchange=w.exchange,
                instrument_token=w.instrument_token,
                lot_size=w.lot_size,
                is_active=w.is_active,
                sector=w.sector,
            )
            for w in watchlist
        ]


@router.post("/", response_model=WatchlistItem)
async def add_to_watchlist(item: WatchlistCreate):
    """Add symbol to watchlist."""
    async with get_database().session() as session:
        repo = WatchlistRepository(session)

        existing = await repo.get_by_symbol(item.symbol.upper())
        if existing:
            # Update existing
            existing.is_active = True
            existing.name = item.name or existing.name
            existing.instrument_token = item.instrument_token or existing.instrument_token
            existing.lot_size = item.lot_size
            existing.sector = item.sector or existing.sector
            await session.commit()
            watchlist_item = existing
        else:
            watchlist_item = await repo.create(
                symbol=item.symbol.upper(),
                name=item.name,
                exchange=item.exchange,
                instrument_token=item.instrument_token,
                lot_size=item.lot_size,
                is_active=True,
                sector=item.sector,
            )

        return WatchlistItem(
            symbol=watchlist_item.symbol,
            name=watchlist_item.name,
            exchange=watchlist_item.exchange,
            instrument_token=watchlist_item.instrument_token,
            lot_size=watchlist_item.lot_size,
            is_active=watchlist_item.is_active,
            sector=watchlist_item.sector,
        )


@router.delete("/{symbol}")
async def remove_from_watchlist(symbol: str):
    """Remove symbol from watchlist (soft delete)."""
    symbol = symbol.upper()
    async with get_database().session() as session:
        repo = WatchlistRepository(session)
        existing = await repo.get_by_symbol(symbol)

        if not existing:
            raise HTTPException(status_code=404, detail="Symbol not in watchlist")

        existing.is_active = False
        await session.commit()

        return {"status": "removed", "symbol": symbol}


@router.post("/sync")
async def sync_watchlist(name: str = Query("nifty50")):
    """Sync watchlist from predefined list."""
    manager = WatchlistManager()
    symbols = await resolve_watchlist(name=name)
    count = await manager.sync_to_db(symbols, source=name)
    return {"status": "synced", "source": name, "count": count}
