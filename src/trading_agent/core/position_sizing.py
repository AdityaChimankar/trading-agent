"""Position sizing and risk management."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True)
class PositionSizingConfig:
    """Configuration for position sizing."""

    # Capital allocation
    max_portfolio_risk_pct: float = 0.06  # 6% max portfolio risk
    max_single_position_pct: float = 0.10  # 10% max per position
    max_sector_exposure_pct: float = 0.20  # 20% max per sector

    # ATR-based sizing
    atr_multiplier: float = 1.5  # Stop distance = ATR * multiplier
    risk_per_trade_pct: float = 0.02  # 2% risk per trade
    min_atr_percentile: float = 0.25  # Min ATR percentile for sizing
    max_atr_percentile: float = 0.75  # Max ATR percentile for sizing

    # Take profit
    use_take_profit: bool = False  # Disabled by default (per research)
    take_profit_multiplier: float = 2.0  # TP = entry + multiplier * risk

    # Volatility adjustment
    volatility_lookback: int = 252  # Days for ATR percentile calc
    reduce_size_high_vol: bool = True  # Reduce size when ATR is high

    # Limits
    min_position_value: float = 5000  # Minimum position value (INR)
    max_positions: int = 10  # Maximum concurrent positions
    lot_size_multiplier: int = 1  # Round to lot size


DEFAULT_CONFIG = PositionSizingConfig()


@dataclass(frozen=True)
class PositionSizeResult:
    """Result of position size calculation."""

    shares: int
    position_value: float
    risk_amount: float
    stop_price: float
    take_profit_price: float | None
    atr_value: float
    atr_percentile: float
    risk_pct_of_portfolio: float


def calculate_atr_percentile(
    atr_values: NDArray[np.float64],
    current_atr: float,
    lookback: int = 252,
) -> float:
    """Calculate current ATR percentile within lookback window."""
    lookback = min(lookback, len(atr_values))

    recent_atr = atr_values[-lookback:]
    valid_atr = recent_atr[~np.isnan(recent_atr)]

    if len(valid_atr) == 0:
        return 0.5

    return float(np.searchsorted(np.sort(valid_atr), current_atr) / len(valid_atr))


def round_to_tick(price: float, tick: float, away_from: float | None = None) -> float:
    """Snap a price to the exchange tick.

    With ``away_from`` (the entry), round away from it, so a stop never ends up
    nearer the entry than planned. Otherwise round to the nearest tick.
    """
    steps = price / tick
    if away_from is None:
        count = round(steps)
    elif price < away_from:
        count = math.floor(steps + 1e-9)
    else:
        count = math.ceil(steps - 1e-9)
    return round(count * tick, 2)


def calculate_position_size(
    entry_price: float,
    atr_value: float,
    portfolio_value: float,
    config: PositionSizingConfig = DEFAULT_CONFIG,
    atr_percentile: float = 0.5,
    existing_positions_value: float = 0.0,
) -> PositionSizeResult:
    """Calculate position size based on ATR and risk parameters.

    Args:
        entry_price: Current entry price
        atr_value: Current ATR value
        portfolio_value: Total portfolio value
        config: Position sizing configuration
        atr_percentile: Current ATR percentile (0-1)
        existing_positions_value: Value of existing positions

    Returns:
        PositionSizeResult with calculated sizes and levels.
    """
    if entry_price <= 0 or atr_value <= 0 or portfolio_value <= 0:
        return PositionSizeResult(
            shares=0,
            position_value=0.0,
            risk_amount=0.0,
            stop_price=0.0,
            take_profit_price=None,
            atr_value=atr_value,
            atr_percentile=atr_percentile,
            risk_pct_of_portfolio=0.0,
        )

    # Base risk per trade
    risk_amount = portfolio_value * config.risk_per_trade_pct

    # Stop distance
    stop_distance = atr_value * config.atr_multiplier
    stop_price = entry_price - stop_distance

    # Adjust risk for high volatility (reduce size when ATR is high)
    vol_adjustment = 1.0
    if config.reduce_size_high_vol:
        if atr_percentile > config.max_atr_percentile:
            # Linearly reduce from 1.0 to 0.5 as percentile goes from 0.75 to 1.0
            vol_adjustment = max(0.5, 1.0 - (atr_percentile - config.max_atr_percentile) * 2)
        elif atr_percentile < config.min_atr_percentile:
            # Increase size slightly for low volatility
            vol_adjustment = min(1.2, 1.0 + (config.min_atr_percentile - atr_percentile))

    risk_amount *= vol_adjustment

    # Calculate shares
    shares = int(risk_amount / stop_distance) if stop_distance > 0 else 0

    # Apply lot size rounding
    if config.lot_size_multiplier > 1:
        shares = (shares // config.lot_size_multiplier) * config.lot_size_multiplier

    # Check minimum position value
    position_value = shares * entry_price
    if position_value < config.min_position_value:
        shares = 0
        position_value = 0.0
        risk_amount = 0.0

    # Check portfolio risk limit
    max_risk = portfolio_value * config.max_portfolio_risk_pct
    current_risk = existing_positions_value * config.risk_per_trade_pct  # Approximate
    if risk_amount + current_risk > max_risk:
        # Scale down
        scale = max(0.0, (max_risk - current_risk) / risk_amount) if risk_amount > 0 else 0.0
        shares = int(shares * scale)
        position_value = shares * entry_price
        risk_amount = position_value * (stop_distance / entry_price) if entry_price > 0 else 0.0

    # Check single position limit
    max_position_value = portfolio_value * config.max_single_position_pct
    if position_value > max_position_value:
        shares = int(max_position_value / entry_price)
        if config.lot_size_multiplier > 1:
            shares = (shares // config.lot_size_multiplier) * config.lot_size_multiplier
        position_value = shares * entry_price
        risk_amount = position_value * (stop_distance / entry_price) if entry_price > 0 else 0.0

    # Take profit
    take_profit_price = None
    if config.use_take_profit and stop_distance > 0:
        take_profit_price = entry_price + (stop_distance * config.take_profit_multiplier)

    risk_pct = risk_amount / portfolio_value if portfolio_value > 0 else 0.0

    return PositionSizeResult(
        shares=shares,
        position_value=position_value,
        risk_amount=risk_amount,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
        atr_value=atr_value,
        atr_percentile=atr_percentile,
        risk_pct_of_portfolio=risk_pct,
    )


def calculate_kelly_fraction(
    win_rate: float,
    avg_win: float,
    avg_loss: float,
) -> float:
    """Calculate Kelly Criterion fraction.

    Args:
        win_rate: Probability of winning (0-1)
        avg_win: Average winning trade return
        avg_loss: Average losing trade return (positive value)

    Returns:
        Kelly fraction (can be negative if negative expectancy)
    """
    if avg_loss <= 0 or win_rate <= 0 or win_rate >= 1:
        return 0.0

    # Kelly = (bp - q) / b where b = avg_win/avg_loss, p = win_rate, q = 1-win_rate
    b = avg_win / avg_loss
    p = win_rate
    q = 1 - win_rate

    kelly = (b * p - q) / b
    return max(0.0, min(1.0, kelly))


def calculate_optimal_f(
    returns: NDArray[np.float64],
    max_fraction: float = 0.25,
) -> float:
    """Calculate Optimal f (Ralph Vince) for position sizing.

    Args:
        returns: Array of trade returns (as decimals, e.g., 0.05 for 5%)
        max_fraction: Maximum fraction to risk

    Returns:
        Optimal fraction of capital to risk per trade
    """
    if len(returns) < 10:
        return 0.0

    # Only consider losing trades for risk calculation
    losses = returns[returns < 0]
    if len(losses) == 0:
        return max_fraction

    # Optimal f = -mean(losses) / max(losses) * (wins / total) approximation
    # More precise: find f that maximizes geometric mean
    worst_loss = abs(np.min(losses))
    if worst_loss == 0:
        return max_fraction

    win_rate = np.mean(returns > 0)
    avg_win = np.mean(returns[returns > 0]) if np.any(returns > 0) else 0
    avg_loss = abs(np.mean(losses))

    # Simplified optimal f
    if avg_loss > 0:
        f = (win_rate * avg_win) / (avg_loss * (1 - win_rate)) if win_rate < 1 else max_fraction
        return max(0.0, min(max_fraction, f))

    return 0.0


@dataclass(frozen=True)
class PositionPlan:
    """Position plan with portfolio-level adjustments."""
    
    symbol: str
    action: str
    entry_price: float
    approved_size: int
    approved_value: float
    blocked: bool
    block_reason: str | None
    total_exposure_pct: float
    total_risk_used_pct: float
    base_size: int
    base_value: float
    reserved_value: float = 0.0
    reserved_risk_amount: float = 0.0


def validate_position(
    symbol: str,
    action: Literal["BUY", "SELL"],
    shares: int,
    price: float,
    portfolio_value: float,
    existing_positions: dict[str, dict],
    config: PositionSizingConfig = DEFAULT_CONFIG,
) -> tuple[bool, str]:
    """Validate a position against risk limits.

    Returns:
        Tuple of (is_valid, reason_if_invalid)
    """
    if shares <= 0:
        return False, "Invalid share count"

    position_value = shares * price

    # Check minimum position value
    if position_value < config.min_position_value:
        return (
            False,
            f"Position value {position_value:.0f} below minimum {config.min_position_value:.0f}",
        )

    # Check max positions
    if len(existing_positions) >= config.max_positions and symbol not in existing_positions:
        return False, f"Maximum positions ({config.max_positions}) reached"

    # Check single position limit
    if position_value > portfolio_value * config.max_single_position_pct:
        return False, f"Position exceeds {config.max_single_position_pct:.0%} portfolio limit"

    # Check portfolio risk (approximate)
    total_risk = sum(
        pos.get("risk_amount", pos["shares"] * pos["entry_price"] * 0.02)
        for pos in existing_positions.values()
    )
    new_risk = position_value * 0.02  # Approximate
    if total_risk + new_risk > portfolio_value * config.max_portfolio_risk_pct:
        return False, f"Portfolio risk would exceed {config.max_portfolio_risk_pct:.0%}"

    return True, "OK"
