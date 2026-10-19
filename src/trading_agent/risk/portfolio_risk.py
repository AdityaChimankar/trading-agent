"""Portfolio risk management."""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import SimpleNamespace

import pandas as pd
import numpy as np

from trading_agent.core.logging_config import get_logger
from trading_agent.core.position_sizing import (
    PositionSizingConfig,
    PositionSizeResult,
    PositionPlan,
    calculate_position_size,
    validate_position,
    DEFAULT_CONFIG,
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


# ============================================================
# Persisted paper-book sizing helpers (not broker order routing)
# ============================================================

def _get_risk_config() -> SimpleNamespace:
    """Use the same portfolio caps as the paper-entry API."""
    from trading_agent.core.config import get_settings
    limits = get_settings().paper
    return SimpleNamespace(
        total_risk_budget_pct=limits.max_risk_pct * 100,
        max_cluster_exposure_pct=20.0,
        max_total_exposure_pct=limits.max_exposure_pct * 100,
    )


def compute_correlation_matrix(symbols: list[str], lookback: int | None = None) -> pd.DataFrame:
    """Compute correlation matrix for symbols."""
    if not symbols:
        return pd.DataFrame()

    from trading_agent.core.config import get_settings
    from trading_agent.storage.parquet_store import get_parquet_store
    series = {}
    for symbol in symbols:
        frame = get_parquet_store().read_latest(symbol, get_settings().storage.candle_interval, n=lookback or 252)
        if not frame.empty:
            series[symbol] = frame.set_index("timestamp")["close"].pct_change(fill_method=None)
    # Unknown correlation stays NaN; it is not evidence of diversification.
    return pd.DataFrame(series).corr(min_periods=20).reindex(index=symbols, columns=symbols)


def get_open_positions() -> list[dict]:
    """Read the supported SQLite ledger; database errors must not imply an empty book."""
    from trading_agent.storage.db import get_connection
    connection = get_connection()
    try:
        return [dict(row) for row in connection.execute("SELECT * FROM open_positions WHERE lower(status) = 'open'")]
    finally:
        connection.close()


def calculate_position(symbol: str, action: str, capital: float) -> PositionSizeResult:
    """Calculate base position size (per-trade risk sizing)."""
    from trading_agent.core.config import get_settings
    from trading_agent.core.signals import prepare_symbol_data
    from trading_agent.core.position_sizing import calculate_atr_percentile
    from trading_agent.storage.parquet_store import get_parquet_store
    if action not in {"BUY", "SELL"}:
        raise ValueError("Action must be BUY or SELL")
    frame = get_parquet_store().read_latest(symbol, get_settings().storage.candle_interval, n=300)
    if len(frame) < 30:
        raise ValueError(f"Insufficient stored candles to size {symbol}")
    prepared = prepare_symbol_data(frame)
    entry_price = float(frame["close"].iloc[-1])
    atr_values = prepared["atrs"]
    atr_value = float(atr_values[-1])
    if not np.isfinite(atr_value) or atr_value <= 0:
        raise ValueError(f"Invalid ATR for {symbol}")
    atr_percentile = calculate_atr_percentile(atr_values, atr_value)
    
    result = calculate_position_size(
        entry_price=entry_price,
        atr_value=atr_value,
        portfolio_value=capital,
        config=DEFAULT_CONFIG,
        atr_percentile=atr_percentile,
        existing_positions_value=0.0,
    )
    if action == "SELL":
        result = replace(result, stop_price=2 * entry_price - result.stop_price,
                         take_profit_price=None if result.take_profit_price is None else 2 * entry_price - result.take_profit_price)
    return result


def calculate_portfolio_adjusted_position(
    symbol: str,
    action: str,
    capital: float,
    reserved_value: float = 0.0,
    reserved_risk_amount: float = 0.0,
) -> PositionPlan:
    """
    Calculate position size with portfolio-level exposure and risk caps.
    
    Applies:
    - Max total exposure cap (50% of capital)
    - Max portfolio risk budget (6% of capital)
    - Reserved value/risk from prior candidates
    """
    config = _get_risk_config()
    
    # Get current open positions
    open_positions = get_open_positions()
    
    # Calculate base position (per-trade risk sizing)
    base_result = calculate_position(symbol, action, capital)
    base_size = base_result.shares
    base_value = base_result.position_value
    base_risk = base_result.risk_amount
    entry_price = base_value / base_size if base_size > 0 else 0.0
    
    if base_size == 0:
        return PositionPlan(
            symbol=symbol,
            action=action,
            entry_price=entry_price,
            approved_size=0,
            approved_value=0.0,
            blocked=True,
            block_reason="base_size_zero",
            total_exposure_pct=0.0,
            total_risk_used_pct=0.0,
            base_size=0,
            base_value=0.0,
        )
    
    # Calculate current exposure from open positions
    current_exposure = sum(p.get("position_value", 0) for p in open_positions)
    current_risk = sum(p.get("risk_amount", 0) for p in open_positions)
    
    # Add reserved amounts
    total_reserved_exposure = current_exposure + reserved_value
    total_reserved_risk = current_risk + reserved_risk_amount
    
    # Check exposure cap (50% of capital)
    max_exposure = capital * (config.max_total_exposure_pct / 100.0)
    available_exposure = max_exposure - total_reserved_exposure
    
    # Check risk budget (6% of capital)
    max_risk_budget = capital * (config.total_risk_budget_pct / 100.0)
    available_risk = max_risk_budget - total_reserved_risk
    
    # Calculate exposure and risk percentages
    total_exposure_pct = (total_reserved_exposure / capital * 100) if capital > 0 else 0.0
    total_risk_used_pct = (total_reserved_risk / capital * 100) if capital > 0 else 0.0
    
    # Determine if blocked and by what
    blocked = False
    block_reason = None
    approved_size = base_size
    approved_value = base_value
    
    # Check exposure cap
    if available_exposure <= 0:
        blocked = True
        block_reason = f"exposure_cap_{config.max_total_exposure_pct}"
        approved_size = 0
        approved_value = 0.0
    else:
        # Scale down if base position exceeds available exposure
        if base_value > available_exposure:
            scale = available_exposure / base_value
            approved_size = int(base_size * scale)
            approved_value = approved_size * entry_price
            if approved_size == 0:
                blocked = True
                block_reason = "exposure_cap_insufficient"
    
    # Check risk budget
    if not blocked and available_risk <= 0:
        blocked = True
        block_reason = "risk_budget_exhausted"
        approved_size = 0
        approved_value = 0.0
    elif not blocked:
        # Check if base risk exceeds available risk
        if base_risk > available_risk:
            scale = available_risk / base_risk
            approved_size = min(approved_size, int(base_size * scale))
            approved_value = approved_size * entry_price
            if approved_size == 0:
                blocked = True
                block_reason = "risk_budget_insufficient"
    
    if any(p["symbol"] == symbol for p in open_positions):
        blocked, block_reason, approved_size, approved_value = True, "symbol_already_open", 0, 0.0

    return PositionPlan(
        symbol=symbol,
        action=action,
        entry_price=entry_price,
        approved_size=approved_size,
        approved_value=approved_value,
        blocked=blocked,
        block_reason=block_reason,
        total_exposure_pct=total_exposure_pct,
        total_risk_used_pct=total_risk_used_pct,
        base_size=base_size,
        base_value=base_value,
        reserved_value=reserved_value,
        reserved_risk_amount=reserved_risk_amount,
    )


def exposure_summary(capital: float) -> dict:
    """Get exposure summary for the wallet/API."""
    open_positions = get_open_positions()
    
    committed_notional = sum(p.get("position_value", 0) for p in open_positions)
    
    # Calculate free notional based on 50% cap
    max_exposure = capital * _get_risk_config().max_total_exposure_pct / 100
    free_notional = max(0.0, max_exposure - committed_notional)
    exposure_pct = (committed_notional / capital * 100) if capital > 0 else 0.0
    
    return {
        "committed_notional": committed_notional,
        "free_notional": free_notional,
        "exposure_pct": round(exposure_pct, 2),
    }
