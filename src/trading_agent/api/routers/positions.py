"""Positions router."""

from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing import Literal
from sqlalchemy import select, func, text, update
from sqlalchemy.exc import SQLAlchemyError
from trading_agent.core.config import get_settings
from typing import Optional, List
from datetime import UTC, datetime

from trading_agent.storage.db import get_database, PositionRepository, Position
from trading_agent.risk.controls import audit, entry_block_reason
from trading_agent.risk.ledger_guard import SELECT_LEDGER, ledger_state

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
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")
    symbol: str = Field(min_length=1, max_length=20, pattern=r"^[A-Za-z0-9&_.-]+$")
    action: Literal["BUY", "SELL"]
    entry_price: float = Field(gt=0)
    quantity: int = Field(gt=0, strict=True)
    stop_loss: float = Field(gt=0)
    take_profit: float | None = Field(default=None, gt=0)
    strategy: str = Field(default="manual", min_length=1, max_length=50)

    @field_validator("action", "symbol", mode="before")
    @classmethod
    def normalize(cls, value):
        return value.strip().upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def check_bracket(self):
        direction = 1 if self.action == "BUY" else -1
        if direction * (self.entry_price - self.stop_loss) <= 0:
            raise ValueError("Stop loss must be on the loss side of entry")
        if self.take_profit is not None and direction * (self.take_profit - self.entry_price) <= 0:
            raise ValueError("Take profit must be on the profit side of entry")
        return self


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
        limits = get_settings().paper
        if not limits.entries_enabled:
            raise HTTPException(status_code=409, detail="Paper entries disabled by operator")
        if abs(position.entry_price - position.stop_loss) < position.entry_price * limits.min_stop_distance_pct:
            raise HTTPException(status_code=409, detail=(
                f"Stop {position.stop_loss:g} is within {limits.min_stop_distance_pct:.2%} of the entry "
                f"{position.entry_price:g}: too tight to survive the spread; widen it"))
        # Serialize check-and-create in the supported SQLite deployment.
        if session.bind.dialect.name != "sqlite":
            raise HTTPException(status_code=503, detail="Paper entry risk locking requires SQLite")
        try:
            await session.execute(text("BEGIN IMMEDIATE"))
            rows = (await session.execute(text(SELECT_LEDGER))).mappings().all()
            ledger = ledger_state(rows, limits)
        except (SQLAlchemyError, ValueError, TypeError, KeyError) as error:
            raise HTTPException(status_code=503, detail="Paper ledger cannot be verified; new entries blocked") from error
        if not ledger["entries_allowed"]:
            raise HTTPException(status_code=409, detail=ledger["block_reason"])
        # Kill switch, session window, daily trade count, per-symbol re-entry (risk/controls.py).
        session_block = entry_block_reason([dict(row) for row in rows], symbol=position.symbol)
        if session_block:
            raise HTTPException(status_code=409, detail=session_block)
        opened = ledger["positions"]
        equity = ledger["realized_equity"]
        if any(p["symbol"].upper() == position.symbol for p in opened):
            raise HTTPException(status_code=409, detail="Symbol already has an open paper position")
        if equity <= 0 or len(opened) >= limits.max_positions:
            raise HTTPException(status_code=409, detail="Paper capital or position limit reached")
        if position_value > equity * limits.max_single_position_pct:
            raise HTTPException(status_code=409, detail="Paper single-position exposure limit exceeded")
        if sum(p["position_value"] for p in opened) + position_value > equity * limits.max_exposure_pct:
            raise HTTPException(status_code=409, detail="Paper exposure limit exceeded")
        if risk_amount > equity * limits.max_trade_risk_pct or sum(p["risk_amount"] for p in opened) + risk_amount > equity * limits.max_risk_pct:
            raise HTTPException(status_code=409, detail="Paper risk limit exceeded")
        if risk_amount > ledger["daily_risk_headroom"]:
            raise HTTPException(status_code=409, detail=(
                f"Paper worst-case daily loss limit: INR {risk_amount:,.0f} risk at the stop, "
                f"INR {ledger['daily_risk_headroom']:,.0f} left today after realized P&L "
                f"and open positions' risk"))
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
        await session.flush()
        audit("position_opened", position_id=pos.id, symbol=pos.symbol, action=pos.action,
              quantity=pos.quantity, entry_price=pos.entry_price, stop_loss=pos.stop_loss,
              take_profit=pos.take_profit, strategy=pos.strategy)

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
    exit_price: float = Query(..., gt=0, allow_inf_nan=False, description="Exit price"),
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

        result = await session.execute(
            update(Position).where(Position.id == position_id, func.lower(Position.status) == "open")
            .values(status="closed", exit_price=exit_price, exit_time=datetime.now(UTC), pnl=pnl)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise HTTPException(status_code=409, detail="Position was already closed")
        audit("position_closed", position_id=position_id, symbol=pos.symbol, exit_price=exit_price, pnl=pnl)

        return {"position_id": position_id, "exit_price": exit_price, "pnl": pnl}
