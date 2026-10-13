"""Portfolio risk management."""

from __future__ import annotations

from dataclasses import dataclass

from trading_agent.core.logging_config import get_logger
from trading_agent.core.position_sizing import (
    PositionSizingConfig,
    calculate_position_size,
    validate_position,
)

logger = get_logger(__name__)


@dataclass(frozen=True)
class RiskLimits:
    """Portfolio risk limits."""

    max_portfolio_risk_pct: float = 0.06  # 6% max portfolio risk
    max_single_position_pct: float = 0.10  # 10% max per position
    max_sector_exposure_pct: float = 0.20  # 20% max per sector
    max_correlated_exposure_pct: float = 0.15  # 15% max correlated positions
    max_daily_loss_pct: float = 0.03  # 3% max daily loss
    max_drawdown_pct: float = 0.15  # 15% max drawdown
    max_positions: int = 10  # Max concurrent positions
    min_cash_reserve_pct: float = 0.05  # 5% minimum cash


DEFAULT_RISK_LIMITS = RiskLimits()


@dataclass
class PositionRisk:
    """Risk metrics for a single position."""

    symbol: str
    entry_price: float
    current_price: float
    quantity: int
    action: str  # BUY, SELL
    stop_loss: float
    take_profit: float | None
    unrealized_pnl: float
    risk_amount: float
    risk_pct: float
    atr: float
    atr_percentile: float


@dataclass
class PortfolioRisk:
    """Overall portfolio risk metrics."""

    total_value: float
    cash: float
    invested: float
    total_risk: float
    total_risk_pct: float
    daily_pnl: float
    daily_pnl_pct: float
    max_drawdown_pct: float
    positions: list[PositionRisk]
    sector_exposure: dict[str, float]
    risk_limits: RiskLimits
    limit_breaches: list[str]


class PortfolioRiskManager:
    """Manage portfolio-level risk."""

    def __init__(
        self,
        limits: RiskLimits = DEFAULT_RISK_LIMITS,
        position_config: PositionSizingConfig | None = None,
    ):
        self.limits = limits
        self.position_config = position_config or PositionSizingConfig()

        # State
        self.positions: dict[str, PositionRisk] = {}
        self.cash = 0.0
        self.initial_capital = 0.0
        self.peak_equity = 0.0
        self.daily_pnl = 0.0
        self.sector_map: dict[str, str] = {}  # symbol -> sector

    def set_capital(self, initial: float, cash: float | None = None) -> None:
        """Set initial capital and cash."""
        self.initial_capital = initial
        self.cash = cash if cash is not None else initial
        self.peak_equity = initial

    def set_sector_map(self, sector_map: dict[str, str]) -> None:
        """Set symbol to sector mapping."""
        self.sector_map = sector_map

    def add_position(
        self,
        symbol: str,
        action: str,
        entry_price: float,
        quantity: int,
        stop_loss: float,
        take_profit: float | None,
        atr: float,
        atr_percentile: float = 0.5,
        sector: str | None = None,
    ) -> tuple[bool, str]:
        """Add a new position and validate risk."""
        # Validate
        is_valid, reason = validate_position(
            symbol=symbol,
            action=action,
            shares=quantity,
            price=entry_price,
            portfolio_value=self.get_total_value(),
            existing_positions={
                s: {
                    "shares": p.quantity,
                    "entry_price": p.entry_price,
                    "risk_amount": p.risk_amount,
                }
                for s, p in self.positions.items()
            },
            config=self.position_config,
        )

        if not is_valid:
            return False, reason

        # Check sector exposure
        if sector:
            sector_exposure = self._get_sector_exposure(sector)
            new_exposure = (entry_price * quantity) / self.get_total_value()
            if sector_exposure + new_exposure > self.limits.max_sector_exposure_pct:
                return (
                    False,
                    f"Sector exposure would exceed {self.limits.max_sector_exposure_pct:.0%}",
                )

        # Create position risk
        risk_amount = quantity * abs(entry_price - stop_loss)
        risk_pct = risk_amount / self.get_total_value() if self.get_total_value() > 0 else 0

        position = PositionRisk(
            symbol=symbol,
            entry_price=entry_price,
            current_price=entry_price,
            quantity=quantity,
            action=action,
            stop_loss=stop_loss,
            take_profit=take_profit,
            unrealized_pnl=0.0,
            risk_amount=risk_amount,
            risk_pct=risk_pct,
            atr=atr,
            atr_percentile=atr_percentile,
        )

        self.positions[symbol] = position
        self.cash -= entry_price * quantity

        return True, "OK"

    def update_position(self, symbol: str, current_price: float) -> None:
        """Update position with current market price."""
        if symbol not in self.positions:
            return

        pos = self.positions[symbol]
        pos.current_price = current_price

        if pos.action == "BUY":
            pos.unrealized_pnl = (current_price - pos.entry_price) * pos.quantity
        else:
            pos.unrealized_pnl = (pos.entry_price - current_price) * pos.quantity

    def remove_position(self, symbol: str, exit_price: float, realized_pnl: float) -> None:
        """Remove a closed position."""
        if symbol in self.positions:
            pos = self.positions[symbol]
            self.cash += exit_price * pos.quantity
            del self.positions[symbol]

    def get_total_value(self) -> float:
        """Get total portfolio value."""
        invested = sum(p.current_price * p.quantity for p in self.positions.values())
        return self.cash + invested

    def _get_sector_exposure(self, sector: str) -> float:
        """Get current exposure to a sector."""
        total = self.get_total_value()
        if total == 0:
            return 0.0

        exposure = 0.0
        for symbol, pos in self.positions.items():
            if self.sector_map.get(symbol) == sector:
                exposure += pos.current_price * pos.quantity
        return exposure / total

    def check_risk_limits(self) -> PortfolioRisk:
        """Check all risk limits and return portfolio risk."""
        total_value = self.get_total_value()
        invested = sum(p.current_price * p.quantity for p in self.positions.values())

        # Total risk
        total_risk = sum(p.risk_amount for p in self.positions.values())
        total_risk_pct = total_risk / total_value if total_value > 0 else 0

        # Daily PnL
        self.daily_pnl = sum(p.unrealized_pnl for p in self.positions.values())
        daily_pnl_pct = self.daily_pnl / self.initial_capital if self.initial_capital > 0 else 0

        # Drawdown
        self.peak_equity = max(self.peak_equity, total_value)
        drawdown = (
            (self.peak_equity - total_value) / self.peak_equity if self.peak_equity > 0 else 0
        )

        # Sector exposure
        sector_exposure = {}
        for sector in set(self.sector_map.values()):
            sector_exposure[sector] = self._get_sector_exposure(sector)

        # Check limits
        breaches = []
        if total_risk_pct > self.limits.max_portfolio_risk_pct:
            breaches.append(
                f"Portfolio risk {total_risk_pct:.2%} > {self.limits.max_portfolio_risk_pct:.0%}"
            )
        if daily_pnl_pct < -self.limits.max_daily_loss_pct:
            breaches.append(
                f"Daily loss {daily_pnl_pct:.2%} > {self.limits.max_daily_loss_pct:.0%}"
            )
        if drawdown > self.limits.max_drawdown_pct:
            breaches.append(f"Drawdown {drawdown:.2%} > {self.limits.max_drawdown_pct:.0%}")
        if len(self.positions) > self.limits.max_positions:
            breaches.append(f"Positions {len(self.positions)} > {self.limits.max_positions}")

        cash_pct = self.cash / total_value if total_value > 0 else 1.0
        if cash_pct < self.limits.min_cash_reserve_pct:
            breaches.append(f"Cash reserve {cash_pct:.2%} < {self.limits.min_cash_reserve_pct:.0%}")

        return PortfolioRisk(
            total_value=total_value,
            cash=self.cash,
            invested=invested,
            total_risk=total_risk,
            total_risk_pct=total_risk_pct,
            daily_pnl=self.daily_pnl,
            daily_pnl_pct=daily_pnl_pct,
            max_drawdown_pct=drawdown * 100,
            positions=list(self.positions.values()),
            sector_exposure=sector_exposure,
            risk_limits=self.limits,
            limit_breaches=breaches,
        )

    def get_position_size(
        self,
        symbol: str,
        entry_price: float,
        atr: float,
        atr_percentile: float = 0.5,
        action: str = "BUY",
    ) -> int:
        """Calculate position size for a new trade."""
        portfolio_value = self.get_total_value()
        existing_value = sum(p.current_price * p.quantity for p in self.positions.values())

        result = calculate_position_size(
            entry_price=entry_price,
            atr_value=atr,
            portfolio_value=portfolio_value,
            config=self.position_config,
            atr_percentile=atr_percentile,
            existing_positions_value=existing_value,
        )

        return result.shares

    def should_reduce_risk(self) -> bool:
        """Check if portfolio should reduce risk."""
        risk = self.check_risk_limits()
        return len(risk.limit_breaches) > 0

    def get_reduction_orders(self) -> list[dict]:
        """Get orders to reduce risk."""
        risk = self.check_risk_limits()
        orders = []

        # Reduce largest positions first
        sorted_positions = sorted(
            risk.positions,
            key=lambda p: p.current_price * p.quantity,
            reverse=True,
        )

        for pos in sorted_positions:
            if not risk.limit_breaches:
                break

            # Reduce by 50%
            reduce_qty = pos.quantity // 2
            if reduce_qty > 0:
                orders.append(
                    {
                        "symbol": pos.symbol,
                        "action": "SELL" if pos.action == "BUY" else "BUY",
                        "quantity": reduce_qty,
                        "reason": "risk_reduction",
                    }
                )

                # Simulate reduction
                pos.quantity -= reduce_qty
                pos.risk_amount *= 0.5
                risk = self.check_risk_limits()

        return orders
