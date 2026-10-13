"""Position monitoring and management."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum

from trading_agent.core.logging_config import get_logger
from trading_agent.storage.db import Position, PositionRepository, get_database

logger = get_logger(__name__)


class PositionStatus(Enum):
    """Position status."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"
    STOPPED = "STOPPED"
    PENDING = "PENDING"


@dataclass(frozen=True)
class MonitorConfig:
    """Position monitor configuration."""

    check_interval_seconds: int = 30
    trailing_stop_enabled: bool = True
    trailing_stop_atr_mult: float = 1.0
    partial_profit_taking: bool = True
    partial_levels: list[float] = field(default_factory=lambda: [0.5, 1.0, 1.5])  # ATR multiples
    partial_sizes: list[float] = field(default_factory=lambda: [0.3, 0.3, 0.4])  # Fraction to close
    max_hold_minutes: int = 390  # 6.5 hours (full trading day)
    force_close_time: str = "15:20"  # Force close before market close


DEFAULT_MONITOR_CONFIG = MonitorConfig()


@dataclass
class PositionState:
    """Current state of a monitored position."""

    position: Position
    current_price: float
    unrealized_pnl: float
    unrealized_pnl_pct: float
    highest_price: float
    lowest_price: float
    trailing_stop: float | None
    partial_exits: list[dict]
    minutes_held: int
    status: PositionStatus


class PositionMonitor:
    """Monitor and manage open positions."""

    def __init__(
        self,
        config: MonitorConfig = DEFAULT_MONITOR_CONFIG,
        db=None,
    ):
        self.config = config
        self.db = db or get_database()
        self._states: dict[str, PositionState] = {}

    async def load_open_positions(self) -> list[Position]:
        """Load open positions from database."""
        async with self.db.session() as session:
            repo = PositionRepository(session)
            return await repo.get_open()

    async def initialize(self) -> None:
        """Initialize monitor with current open positions."""
        positions = await self.load_open_positions()
        for pos in positions:
            self._states[pos.symbol] = PositionState(
                position=pos,
                current_price=pos.entry_price,
                unrealized_pnl=0.0,
                unrealized_pnl_pct=0.0,
                highest_price=pos.entry_price,
                lowest_price=pos.entry_price,
                trailing_stop=pos.stop_loss,
                partial_exits=[],
                minutes_held=0,
                status=PositionStatus.OPEN,
            )

    def update_price(
        self, symbol: str, price: float, high: float | None = None, low: float | None = None
    ) -> PositionState | None:
        """Update position with latest price."""
        if symbol not in self._states:
            return None

        state = self._states[symbol]
        pos = state.position

        state.current_price = price
        state.highest_price = max(state.highest_price, high or price)
        state.lowest_price = min(state.lowest_price, low or price)

        # Calculate unrealized PnL
        if pos.action == "BUY":
            state.unrealized_pnl = (price - pos.entry_price) * pos.quantity
        else:
            state.unrealized_pnl = (pos.entry_price - price) * pos.quantity

        state.unrealized_pnl_pct = state.unrealized_pnl / (pos.entry_price * pos.quantity) * 100

        # Update trailing stop
        if self.config.trailing_stop_enabled:
            state.trailing_stop = self._calculate_trailing_stop(state)

        # Update minutes held
        state.minutes_held = int((datetime.now(UTC) - pos.entry_time).total_seconds() / 60)

        return state

    def _calculate_trailing_stop(self, state: PositionState) -> float:
        """Calculate trailing stop price."""
        pos = state.position
        atr_mult = self.config.trailing_stop_atr_mult

        # Approximate ATR from entry risk
        entry_risk = abs(pos.entry_price - pos.stop_loss)
        trail_distance = entry_risk * atr_mult

        if pos.action == "BUY":
            # Trail below highest price
            return state.highest_price - trail_distance
        else:
            # Trail above lowest price
            return state.lowest_price + trail_distance

    def check_exits(self, symbol: str) -> list[dict]:
        """Check if position should be exited."""
        if symbol not in self._states:
            return []

        state = self._states[symbol]
        pos = state.position
        exits = []

        # Check stop loss (trailing or fixed)
        stop_price = state.trailing_stop or pos.stop_loss

        if pos.action == "BUY":
            if state.current_price <= stop_price:
                exits.append({"reason": "stop_loss", "price": stop_price, "quantity": pos.quantity})
        elif state.current_price >= stop_price:
            exits.append({"reason": "stop_loss", "price": stop_price, "quantity": pos.quantity})

        # Check take profit
        if pos.take_profit:
            if (
                pos.action == "BUY"
                and state.current_price >= pos.take_profit
                or pos.action == "SELL"
                and state.current_price <= pos.take_profit
            ):
                exits.append(
                    {"reason": "take_profit", "price": pos.take_profit, "quantity": pos.quantity}
                )

        # Check partial profit taking
        if self.config.partial_profit_taking and not state.partial_exits:
            for i, (level_mult, size_frac) in enumerate(
                zip(self.config.partial_levels, self.config.partial_sizes, strict=False)
            ):
                # Calculate target price
                entry_risk = abs(pos.entry_price - pos.stop_loss)
                target_distance = entry_risk * level_mult

                if pos.action == "BUY":
                    target = pos.entry_price + target_distance
                    if state.current_price >= target:
                        qty = int(pos.quantity * size_frac)
                        exits.append(
                            {
                                "reason": f"partial_profit_{i + 1}",
                                "price": target,
                                "quantity": qty,
                            }
                        )
                else:
                    target = pos.entry_price - target_distance
                    if state.current_price <= target:
                        qty = int(pos.quantity * size_frac)
                        exits.append(
                            {
                                "reason": f"partial_profit_{i + 1}",
                                "price": target,
                                "quantity": qty,
                            }
                        )

        # Check max hold time
        if state.minutes_held >= self.config.max_hold_minutes:
            exits.append(
                {
                    "reason": "max_hold_time",
                    "price": state.current_price,
                    "quantity": pos.quantity,
                }
            )

        # Check force close time
        now = datetime.now(UTC)
        force_hour, force_minute = map(int, self.config.force_close_time.split(":"))
        if now.hour == force_hour and now.minute >= force_minute:
            exits.append(
                {
                    "reason": "force_close",
                    "price": state.current_price,
                    "quantity": pos.quantity,
                }
            )

        return exits

    def execute_exit(
        self, symbol: str, exit_price: float, reason: str, quantity: int | None = None
    ) -> bool:
        """Execute an exit (mark position as closed)."""
        if symbol not in self._states:
            return False

        state = self._states[symbol]
        pos = state.position

        qty = quantity or pos.quantity

        # Update position in database
        # This would be done async in practice
        # await self._update_db_position(symbol, exit_price, reason, qty)

        # Update state
        pos.quantity -= qty
        if pos.quantity <= 0:
            pos.status = "CLOSED"
            pos.exit_price = exit_price
            pos.exit_time = datetime.now(UTC)
            pos.pnl = state.unrealized_pnl
            state.status = PositionStatus.CLOSED
            del self._states[symbol]
        else:
            # Partial exit
            state.partial_exits.append(
                {
                    "price": exit_price,
                    "quantity": qty,
                    "reason": reason,
                    "time": datetime.now(UTC),
                }
            )

        return True

    def get_position_state(self, symbol: str) -> PositionState | None:
        """Get current state of a position."""
        return self._states.get(symbol)

    def get_all_states(self) -> dict[str, PositionState]:
        """Get all position states."""
        return self._states.copy()

    def get_summary(self) -> dict:
        """Get portfolio summary."""
        total_unrealized = sum(s.unrealized_pnl for s in self._states.values())
        total_invested = sum(
            s.position.entry_price * s.position.quantity for s in self._states.values()
        )

        return {
            "open_positions": len(self._states),
            "total_invested": total_invested,
            "total_unrealized_pnl": total_unrealized,
            "unrealized_pnl_pct": total_unrealized / total_invested * 100
            if total_invested > 0
            else 0,
            "positions": {
                symbol: {
                    "action": state.position.action,
                    "entry_price": state.position.entry_price,
                    "current_price": state.current_price,
                    "quantity": state.position.quantity,
                    "unrealized_pnl": state.unrealized_pnl,
                    "unrealized_pnl_pct": state.unrealized_pnl_pct,
                    "stop_loss": state.trailing_stop or state.position.stop_loss,
                    "minutes_held": state.minutes_held,
                }
                for symbol, state in self._states.items()
            },
        }
