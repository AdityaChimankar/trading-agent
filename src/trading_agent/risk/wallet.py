"""Paper trading wallet and PnL tracking."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd

from trading_agent.core.logging_config import get_logger
from trading_agent.storage.db import get_database

logger = get_logger(__name__)


@dataclass(frozen=True)
class WalletConfig:
    """Wallet configuration."""

    initial_cash: float = 1000000.0  # 10L INR
    brokerage_per_trade: float = 20.0  # Flat fee per trade
    stt_rate: float = 0.00025  # 0.025% STT on sell side
    exchange_txn_charge: float = 0.0000345  # 0.00345%
    gst_rate: float = 0.18  # 18% GST on brokerage
    sebi_charge: float = 0.000001  # 0.0001%
    stamp_duty: float = 0.00003  # 0.003% on buy side


DEFAULT_WALLET_CONFIG = WalletConfig()


@dataclass
class TradeRecord:
    """Record of a completed trade."""

    symbol: str
    entry_time: datetime
    exit_time: datetime
    action: str  # BUY, SELL
    entry_price: float
    exit_price: float
    quantity: int
    gross_pnl: float
    charges: float
    net_pnl: float
    holding_minutes: int


@dataclass
class WalletSnapshot:
    """Wallet snapshot at a point in time."""

    timestamp: datetime
    cash: float
    invested: float
    total_value: float
    daily_pnl: float
    total_pnl: float
    open_positions: int


class PaperWallet:
    """Paper trading wallet with full charge calculation."""

    def __init__(
        self,
        config: WalletConfig = DEFAULT_WALLET_CONFIG,
        initial_cash: float | None = None,
        db=None,
    ):
        self.config = config
        self.db = db or get_database()

        self.cash = initial_cash or config.initial_cash
        self.initial_cash = self.cash
        self.invested = 0.0
        self.total_pnl = 0.0
        self.daily_pnl = 0.0
        self.last_snapshot_date = datetime.now(UTC).date()

        # Trade history
        self.trades: list[TradeRecord] = []
        self.snapshots: list[WalletSnapshot] = []

        # Position tracking
        self.open_positions: dict[str, dict] = {}

    def _calculate_charges(
        self,
        action: str,
        price: float,
        quantity: int,
    ) -> float:
        """Calculate total charges for a trade."""
        turnover = price * quantity

        # Brokerage
        brokerage = self.config.brokerage_per_trade

        # STT (only on sell side for delivery, both for intraday)
        stt = turnover * self.config.stt_rate if action == "SELL" else 0

        # Exchange transaction charges
        exchange_charges = turnover * self.config.exchange_txn_charge

        # GST on brokerage + exchange charges
        gst = (brokerage + exchange_charges) * self.config.gst_rate

        # SEBI charges
        sebi = turnover * self.config.sebi_charge

        # Stamp duty (on buy side)
        stamp = turnover * self.config.stamp_duty if action == "BUY" else 0

        return brokerage + stt + exchange_charges + gst + sebi + stamp

    def buy(
        self,
        symbol: str,
        price: float,
        quantity: int,
        timestamp: datetime | None = None,
    ) -> tuple[bool, str]:
        """Execute a buy order."""
        ts = timestamp or datetime.now(UTC)

        # Calculate cost
        cost = price * quantity
        charges = self._calculate_charges("BUY", price, quantity)
        total_cost = cost + charges

        if total_cost > self.cash:
            return False, f"Insufficient cash: need {total_cost:.2f}, have {self.cash:.2f}"

        # Execute
        self.cash -= total_cost
        self.invested += cost

        # Track position
        if symbol not in self.open_positions:
            self.open_positions[symbol] = {
                "quantity": 0,
                "avg_price": 0.0,
                "entry_time": ts,
                "action": "BUY",
            }

        pos = self.open_positions[symbol]
        # Update average price
        total_qty = pos["quantity"] + quantity
        pos["avg_price"] = (pos["avg_price"] * pos["quantity"] + price * quantity) / total_qty
        pos["quantity"] = total_qty

        return True, "OK"

    def sell(
        self,
        symbol: str,
        price: float,
        quantity: int,
        timestamp: datetime | None = None,
    ) -> tuple[bool, str]:
        """Execute a sell order."""
        ts = timestamp or datetime.now(UTC)

        if symbol not in self.open_positions:
            return False, f"No open position in {symbol}"

        pos = self.open_positions[symbol]
        if pos["quantity"] < quantity:
            return False, f"Insufficient quantity: have {pos['quantity']}, need {quantity}"

        # Calculate proceeds
        proceeds = price * quantity
        charges = self._calculate_charges("SELL", price, quantity)
        net_proceeds = proceeds - charges

        # Calculate PnL
        entry_price = pos["avg_price"]
        gross_pnl = (price - entry_price) * quantity
        net_pnl = gross_pnl - charges

        # Execute
        self.cash += net_proceeds
        self.invested -= entry_price * quantity
        self.total_pnl += net_pnl
        self.daily_pnl += net_pnl

        # Record trade
        holding_minutes = int((ts - pos["entry_time"]).total_seconds() / 60)
        trade = TradeRecord(
            symbol=symbol,
            entry_time=pos["entry_time"],
            exit_time=ts,
            action="BUY",  # Original action was BUY
            entry_price=entry_price,
            exit_price=price,
            quantity=quantity,
            gross_pnl=gross_pnl,
            charges=charges,
            net_pnl=net_pnl,
            holding_minutes=holding_minutes,
        )
        self.trades.append(trade)

        # Update position
        pos["quantity"] -= quantity
        if pos["quantity"] <= 0:
            del self.open_positions[symbol]

        return True, "OK"

    def short_sell(
        self,
        symbol: str,
        price: float,
        quantity: int,
        timestamp: datetime | None = None,
    ) -> tuple[bool, str]:
        """Execute a short sell order."""
        ts = timestamp or datetime.now(UTC)

        # For short selling, we receive cash but have obligation
        proceeds = price * quantity
        charges = self._calculate_charges("SELL", price, quantity)
        net_proceeds = proceeds - charges

        # Margin requirement (simplified - 20% of value)
        margin = proceeds * 0.2

        if margin > self.cash:
            return False, f"Insufficient margin: need {margin:.2f}, have {self.cash:.2f}"

        self.cash += net_proceeds - margin  # Net cash after margin blocked
        self.invested += proceeds  # Notional exposure

        if symbol not in self.open_positions:
            self.open_positions[symbol] = {
                "quantity": 0,
                "avg_price": 0.0,
                "entry_time": ts,
                "action": "SELL",
                "margin_blocked": 0.0,
            }

        pos = self.open_positions[symbol]
        total_qty = pos["quantity"] + quantity
        pos["avg_price"] = (pos["avg_price"] * pos["quantity"] + price * quantity) / total_qty
        pos["quantity"] = total_qty
        pos["margin_blocked"] += margin

        return True, "OK"

    def cover_short(
        self,
        symbol: str,
        price: float,
        quantity: int,
        timestamp: datetime | None = None,
    ) -> tuple[bool, str]:
        """Cover a short position."""
        ts = timestamp or datetime.now(UTC)

        if symbol not in self.open_positions:
            return False, f"No open short position in {symbol}"

        pos = self.open_positions[symbol]
        if pos["action"] != "SELL" or pos["quantity"] < quantity:
            return False, "Invalid short position"

        # Calculate PnL for short
        proceeds = price * quantity
        charges = self._calculate_charges("BUY", price, quantity)  # Buy to cover
        net_cost = proceeds + charges  # Cost to buy back

        entry_price = pos["avg_price"]
        gross_pnl = (entry_price - price) * quantity
        net_pnl = gross_pnl - charges

        # Release margin
        margin_released = pos["margin_blocked"] * (quantity / pos["quantity"])

        self.cash += margin_released - net_cost
        self.invested -= entry_price * quantity
        self.total_pnl += net_pnl
        self.daily_pnl += net_pnl

        # Record trade
        holding_minutes = int((ts - pos["entry_time"]).total_seconds() / 60)
        trade = TradeRecord(
            symbol=symbol,
            entry_time=pos["entry_time"],
            exit_time=ts,
            action="SELL",  # Original action was SELL (short)
            entry_price=entry_price,
            exit_price=price,
            quantity=quantity,
            gross_pnl=gross_pnl,
            charges=charges,
            net_pnl=net_pnl,
            holding_minutes=holding_minutes,
        )
        self.trades.append(trade)

        pos["quantity"] -= quantity
        pos["margin_blocked"] -= margin_released
        if pos["quantity"] <= 0:
            del self.open_positions[symbol]

        return True, "OK"

    def mark_to_market(self, prices: dict[str, float]) -> float:
        """Mark open positions to market and return unrealized PnL."""
        unrealized = 0.0

        for symbol, pos in self.open_positions.items():
            if symbol not in prices:
                continue

            current_price = prices[symbol]
            entry_price = pos["avg_price"]
            qty = pos["quantity"]

            if pos["action"] == "BUY":
                unrealized += (current_price - entry_price) * qty
            else:
                unrealized += (entry_price - current_price) * qty

        return unrealized

    def get_total_value(self, prices: dict[str, float] | None = None) -> float:
        """Get total portfolio value."""
        if prices:
            unrealized = self.mark_to_market(prices)
            return self.cash + self.invested + unrealized
        return self.cash + self.invested

    def take_snapshot(self, timestamp: datetime | None = None) -> WalletSnapshot:
        """Take a wallet snapshot."""
        ts = timestamp or datetime.now(UTC)

        # Check for new day
        if ts.date() != self.last_snapshot_date:
            self.daily_pnl = 0.0
            self.last_snapshot_date = ts.date()

        total_value = self.get_total_value()

        snapshot = WalletSnapshot(
            timestamp=ts,
            cash=self.cash,
            invested=self.invested,
            total_value=total_value,
            daily_pnl=self.daily_pnl,
            total_pnl=self.total_pnl,
            open_positions=len(self.open_positions),
        )

        self.snapshots.append(snapshot)

        # Persist to database (async in practice)
        # await self._save_snapshot(snapshot)

        return snapshot

    def get_performance_metrics(self) -> dict:
        """Get wallet performance metrics."""
        if not self.trades:
            return {
                "total_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "total_pnl": 0.0,
                "total_pnl_pct": 0.0,
                "win_rate": 0.0,
                "avg_win": 0.0,
                "avg_loss": 0.0,
                "largest_win": 0.0,
                "largest_loss": 0.0,
                "profit_factor": 0.0,
                "max_drawdown_pct": 0.0,
                "avg_holding_minutes": 0.0,
                "total_charges": 0.0,
                "current_cash": self.cash,
                "current_invested": self.invested,
                "open_positions": len(self.open_positions),
            }

        pnls = [t.net_pnl for t in self.trades]
        wins = [p for p in pnls if p > 0]
        # Break-even trades are not losses and must not create a zero divisor.
        losses = [p for p in pnls if p < 0]

        # Equity curve
        equity = [self.initial_cash]
        for t in self.trades:
            equity.append(equity[-1] + t.net_pnl)

        peak = pd.Series(equity).cummax()
        drawdown = (pd.Series(equity) - peak) / peak

        return {
            "total_trades": len(self.trades),
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate": len(wins) / len(pnls) * 100 if pnls else 0,
            "total_pnl": sum(pnls),
            "total_pnl_pct": sum(pnls) / self.initial_cash * 100,
            "avg_win": sum(wins) / len(wins) if wins else 0,
            "avg_loss": sum(losses) / len(losses) if losses else 0,
            "largest_win": max(wins) if wins else 0,
            "largest_loss": min(losses) if losses else 0,
            "profit_factor": abs(sum(wins) / sum(losses)) if losses else float("inf"),
            "max_drawdown_pct": abs(drawdown.min()) * 100 if len(drawdown) > 0 else 0,
            "avg_holding_minutes": sum(t.holding_minutes for t in self.trades) / len(self.trades),
            "total_charges": sum(t.charges for t in self.trades),
            "current_cash": self.cash,
            "current_invested": self.invested,
            "open_positions": len(self.open_positions),
        }

    def get_trade_history(self, limit: int = 100) -> list[TradeRecord]:
        """Get recent trade history."""
        return self.trades[-limit:]

    def get_equity_curve(self) -> pd.DataFrame:
        """Get equity curve as DataFrame."""
        if not self.snapshots:
            return pd.DataFrame()

        df = pd.DataFrame(
            [
                {
                    "timestamp": s.timestamp,
                    "total_value": s.total_value,
                    "cash": s.cash,
                    "invested": s.invested,
                    "daily_pnl": s.daily_pnl,
                    "total_pnl": s.total_pnl,
                }
                for s in self.snapshots
            ]
        )

        return df.set_index("timestamp")
