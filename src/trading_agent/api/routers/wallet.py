"""Wallet router."""

from fastapi import APIRouter, Query
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

router = APIRouter()


class WalletSnapshot(BaseModel):
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
    profit_factor: float
    max_drawdown_pct: float
    avg_holding_minutes: float
    total_charges: float
    current_cash: float
    current_invested: float
    open_positions: int


# Global wallet instance (would be properly managed in production)
_wallet_instance = None


def get_wallet():
    global _wallet_instance
    if _wallet_instance is None:
        from trading_agent.risk.wallet import PaperWallet

        _wallet_instance = PaperWallet()
    return _wallet_instance


@router.get("/summary", response_model=WalletSnapshot)
async def get_wallet_summary():
    """Get current wallet snapshot."""
    wallet = get_wallet()
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
    wallet = get_wallet()
    metrics = wallet.get_performance_metrics()
    return WalletMetrics(**metrics)


@router.get("/trades", response_model=List[TradeRecord])
async def get_trade_history(limit: int = Query(100, ge=1, le=1000)):
    """Get trade history."""
    wallet = get_wallet()
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
    wallet = get_wallet()
    df = wallet.get_equity_curve()

    if df.empty:
        return {"data": []}

    return {
        "data": [
            {"timestamp": idx.isoformat(), "value": row["total_value"]}
            for idx, row in df.iterrows()
        ]
    }


@router.post("/reset")
async def reset_wallet(initial_cash: float = Query(1000000)):
    """Reset wallet to initial state."""
    global _wallet_instance
    from trading_agent.risk.wallet import PaperWallet

    _wallet_instance = PaperWallet(initial_cash=initial_cash)
    return {"status": "reset", "initial_cash": initial_cash}
