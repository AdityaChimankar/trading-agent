"""Wallet router."""

from fastapi import APIRouter, Query, HTTPException
from trading_agent.risk.paper_ledger import load_paper_book
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

router = APIRouter()


class WalletSnapshot(BaseModel):
    accounting_basis: str = "gross_entry_cost; fees, slippage and unrealized PnL excluded"
    timestamp: datetime
    cash: float
    invested: float
    total_value: float
    daily_pnl: float
    total_pnl: float
    open_positions: int


class TradeRecord(BaseModel):
    symbol: str
    entry_time: datetime
    exit_time: datetime
    action: str
    entry_price: float
    exit_price: float
    quantity: int
    gross_pnl: float
    charges: float
    net_pnl: float
    holding_minutes: int


class WalletMetrics(BaseModel):
    accounting_basis: str = "gross_entry_cost; fees, slippage and unrealized PnL excluded"
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_pnl: float
    total_pnl_pct: float
    avg_win: float
    avg_loss: float
    largest_win: float
    largest_loss: float
    profit_factor: float | None
    max_drawdown_pct: float
    avg_holding_minutes: float
    total_charges: float
    current_cash: float
    current_invested: float
    open_positions: int


@router.get("/summary", response_model=WalletSnapshot)
async def get_wallet_summary():
    """Get current wallet snapshot."""
    wallet = await load_paper_book()
    snapshot = wallet.take_snapshot()
    return WalletSnapshot(
        timestamp=snapshot.timestamp,
        cash=snapshot.cash,
        invested=snapshot.invested,
        total_value=snapshot.total_value,
        daily_pnl=snapshot.daily_pnl,
        total_pnl=snapshot.total_pnl,
        open_positions=snapshot.open_positions,
    )


@router.get("/metrics", response_model=WalletMetrics)
async def get_wallet_metrics():
    """Get wallet performance metrics."""
    wallet = await load_paper_book()
    metrics = wallet.get_performance_metrics()
    import math
    if not math.isfinite(metrics["profit_factor"]):
        metrics["profit_factor"] = None
    return WalletMetrics(**metrics)


@router.get("/trades", response_model=List[TradeRecord])
async def get_trade_history(limit: int = Query(100, ge=1, le=1000)):
    """Get trade history."""
    wallet = await load_paper_book()
    trades = wallet.get_trade_history(limit)
    return [
        TradeRecord(
            symbol=t.symbol,
            entry_time=t.entry_time,
            exit_time=t.exit_time,
            action=t.action,
            entry_price=t.entry_price,
            exit_price=t.exit_price,
            quantity=t.quantity,
            gross_pnl=t.gross_pnl,
            charges=t.charges,
            net_pnl=t.net_pnl,
            holding_minutes=t.holding_minutes,
        )
        for t in trades
    ]


@router.get("/equity-curve")
async def get_equity_curve():
    """Get equity curve data."""
    wallet = await load_paper_book()
    balance = wallet.initial_cash
    data = []
    for trade in wallet.trades:
        balance += trade.net_pnl
        data.append({"timestamp": trade.exit_time.isoformat(), "value": balance})
    return {"data": data, "accounting_basis": "realized gross PnL only"}


@router.post("/reset")
async def reset_wallet(initial_cash: float = Query(1000000)):
    """Reset wallet to initial state."""
    raise HTTPException(status_code=409, detail="Persistent paper ledger cannot be reset via API; archive it before starting a new book")
