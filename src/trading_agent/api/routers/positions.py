"""Positions router."""

from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from datetime import UTC, datetime

from trading_agent.storage.db import get_database, PositionRepository, Position

router = APIRouter()


class PositionResponse(BaseModel):
    id: int
    symbol: str
    action: str
    entry_price: float
    entry_time: datetime
    quantity: int
    stop_loss: float
    take_profit: float | None
    strategy: str
    status: str
    exit_price: float | None
    exit_time: datetime | None
    pnl: float | None


class PositionCreate(BaseModel):
    symbol: str
    action: str
    entry_price: float
    quantity: int
    stop_loss: float
    take_profit: float | None = None
    strategy: str = "manual"


@router.get("/", response_model=List[PositionResponse])
async def list_positions(
    status: str = Query("OPEN", description="Filter by status"),
    symbol: str | None = Query(None),
):
    """List positions."""
    async with get_database().session() as session:
        repo = PositionRepository(session)

        if status.lower() == "open":
            positions = await repo.get_open(symbol)
        else:
            positions = await repo.get_by_status(status, symbol)

        return [
            PositionResponse(
                id=p.id,
                symbol=p.symbol,
                action=p.action,
                entry_price=p.entry_price,
                entry_time=p.entry_time,
                quantity=p.quantity,
                stop_loss=p.stop_loss,
                take_profit=p.take_profit,
                strategy=p.strategy,
                status=p.status,
                exit_price=p.exit_price,
                exit_time=p.exit_time,
                pnl=p.pnl,
            )
            for p in positions
        ]


@router.post("/", response_model=PositionResponse)
async def create_position(position: PositionCreate):
    """Create a new paper position."""
    async with get_database().session() as session:
        repo = PositionRepository(session)

        position_value = position.entry_price * position.quantity
        risk_amount = abs(position.entry_price - position.stop_loss) * position.quantity
        pos = await repo.create(
            symbol=position.symbol.upper(),
            action=position.action.upper(),
            entry_price=position.entry_price,
            opened_at=datetime.now(UTC),
            position_size=position.quantity,
            position_value=position_value,
            risk_amount=risk_amount,
            stop_loss=position.stop_loss,
            take_profit=position.take_profit,
            signal_source=position.strategy,
            status="open",
        )

        return PositionResponse(
            id=pos.id,
            symbol=pos.symbol,
            action=pos.action,
            entry_price=pos.entry_price,
            entry_time=pos.entry_time,
            quantity=pos.quantity,
            stop_loss=pos.stop_loss,
            take_profit=pos.take_profit,
            strategy=pos.strategy,
            status=pos.status,
            exit_price=pos.exit_price,
            exit_time=pos.exit_time,
            pnl=pos.pnl,
        )


@router.post("/{position_id}/close")
async def close_position(
    position_id: int,
    exit_price: float = Query(..., description="Exit price"),
):
    """Close a position."""
    async with get_database().session() as session:
        repo = PositionRepository(session)

        pos = await repo.get_by_id(position_id)
        if not pos:
            raise HTTPException(status_code=404, detail="Position not found")

        if pos.status.lower() != "open":
            raise HTTPException(status_code=400, detail="Position already closed")

        # Calculate PnL
        if pos.action == "BUY":
            pnl = (exit_price - pos.entry_price) * pos.quantity
        else:
            pnl = (pos.entry_price - exit_price) * pos.quantity

        # Update position
        pos.status = "closed"
        pos.exit_price = exit_price
        pos.exit_time = datetime.now(UTC)
        pos.pnl = pnl

        await session.commit()

        return {"position_id": position_id, "exit_price": exit_price, "pnl": pnl}
