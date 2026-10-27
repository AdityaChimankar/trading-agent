"""Position monitoring and management."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from enum import Enum
from typing import Any

import pandas as pd

from trading_agent.core.logging_config import get_logger
from trading_agent.storage.db import (
    Position,
    PositionAlert,
    Trade,
    PositionRepository,
    CandleRepository,
    SignalRepository,
    get_database,
)

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


@dataclass(frozen=True)
class PositionStatusResult:
    """Position status result for API/API responses."""
    
    position: dict[str, Any]
    recommendation: str  # SELL, HOLD, STALE, NO_DATA
    alert_kind: str | None  # stop, target, reversal, stale, no_data
    reason: str
    current_price: float | None
    pnl: float | None
    pnl_pct: float | None
    stale: bool
    candle_age_minutes: float | None
    current_value: float | None


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


def _get_attr(obj: Any, key: str, default: Any = None) -> Any:
    """Get attribute from object or dict."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _get_quantity(obj: Any) -> int:
    """Get quantity from object or dict, checking both 'quantity' and 'position_size'."""
    return _get_attr(obj, "quantity") or _get_attr(obj, "position_size") or 0


async def _get_latest_candle(session, symbol: str, interval: str = "5minute"):
    """Prefer the canonical ingest store; retain SQLite fallback for legacy books."""
    from types import SimpleNamespace
    from starlette.concurrency import run_in_threadpool
    from trading_agent.core.config import get_settings
    from trading_agent.storage.parquet_store import get_parquet_store
    interval = get_settings().storage.candle_interval
    frame = await run_in_threadpool(get_parquet_store().read_latest, symbol, interval, n=1)
    if not frame.empty:
        return SimpleNamespace(**frame.iloc[-1].to_dict())
    repo = CandleRepository(session)
    candles = await repo.get_latest(symbol, interval, limit=1)
    return candles[0] if candles else None


async def _get_latest_signal(session, symbol: str, strategy: str | None = None):
    """Get the latest signal for a symbol."""
    repo = SignalRepository(session)
    return await repo.get_latest(symbol, strategy)


async def _get_candle_age_minutes(candle_timestamp: datetime, now: datetime) -> float:
    """Calculate candle age in minutes.
    
    Naive datetimes are assumed to be in IST (consistent with test expectations).
    """
    if candle_timestamp.tzinfo is None:
        # Assume IST for naive datetimes
        candle_timestamp = candle_timestamp.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
    if now.tzinfo is None:
        # Assume IST for naive datetimes
        now = now.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
    return (now - candle_timestamp).total_seconds() / 60.0


def _is_market_hours(dt: datetime) -> bool:
    """Check if datetime is within NSE market hours (9:15-15:30 IST).
    
    Naive datetimes are assumed to be in IST (consistent with test expectations).
    Timezone-aware datetimes are converted to IST.
    """
    if dt.tzinfo is None:
        # Naive datetime - assume IST (consistent with test expectations)
        ist = dt
    else:
        # Timezone-aware - convert to IST (UTC+5:30)
        ist = dt.astimezone(timezone(timedelta(hours=5, minutes=30)))
    
    hour = ist.hour
    minute = ist.minute
    
    if hour < 9 or hour > 15:
        return False
    if hour == 9 and minute < 15:
        return False
    if hour == 15 and minute > 30:
        return False
    if ist.weekday() >= 5:  # Saturday/Sunday
        return False
    return True


async def get_position_status(position_row: Any, now: datetime | None = None, fetch_current_signals: bool = True) -> PositionStatusResult:
    """
    Get position status for a single position.
    
    This is the main function that evaluates a position against current market data
    and returns a PositionStatusResult with recommendation and alert info.
    """
    if now is None:
        now = datetime.now(UTC)
    
    db = get_database()
    
    # Get latest candle for this symbol
    symbol = _get_attr(position_row, "symbol")
    async with db.session() as session:
        candle = await _get_latest_candle(session, symbol)
        signal = await _get_latest_signal(session, symbol) if fetch_current_signals else None
    
    if candle is None:
        # No candle data available
        return PositionStatusResult(
            position={
                "symbol": _get_attr(position_row, "symbol"),
                "action": _get_attr(position_row, "action"),
                "entry_price": _get_attr(position_row, "entry_price"),
                "position_size": _get_quantity(position_row),
                "position_value": _get_attr(position_row, "entry_price") * _get_quantity(position_row),
                "stop_loss": _get_attr(position_row, "stop_loss"),
                "take_profit": _get_attr(position_row, "take_profit"),
            },
            recommendation="NO_DATA",
            alert_kind="no_data",
            reason="No candle data available for position",
            current_price=None,
            pnl=None,
            pnl_pct=None,
            stale=False,
            candle_age_minutes=None,
            current_value=None,
        )
    
    # Calculate candle age
    candle_age = await _get_candle_age_minutes(candle.timestamp, now)
    
    # Check if feed is stale (candle older than 60 minutes during market hours)
    in_session = _is_market_hours(now)
    is_stale = in_session and candle_age > 60
    
    # Use candle's high/low for intrabar stop/target checks
    high = float(candle.high)
    low = float(candle.low)
    close = float(candle.close)
    
    # Calculate PnL based on close price
    action = _get_attr(position_row, "action")
    entry_price = _get_attr(position_row, "entry_price")
    quantity = _get_quantity(position_row)
    stop_loss = _get_attr(position_row, "stop_loss")
    take_profit = _get_attr(position_row, "take_profit")
    
    if action == "BUY":
        pnl = (close - entry_price) * quantity
        current_value = close * quantity
    else:
        pnl = (entry_price - close) * quantity
        current_value = close * quantity
    
    pnl_pct = pnl / (entry_price * quantity) * 100 if quantity > 0 else 0
    
    # Check for signal reversal
    reversal = False
    if fetch_current_signals and signal is not None:
        if action == "BUY" and signal.action == "SELL":
            reversal = True
        elif action == "SELL" and signal.action == "BUY":
            reversal = True
    
    # Determine recommendation and alert_kind
    recommendation = "HOLD"
    alert_kind = None
    reason_parts = []
    
    # Check stop loss using candle's LOW (for BUY) or HIGH (for SELL)
    stop_price = stop_loss
    stop_hit = False
    
    if action == "BUY":
        if low <= stop_price:
            stop_hit = True
            recommendation = "SELL"
            alert_kind = "stop"
            reason_parts.append(f"intrabar low {low:.1f} pierced stop {stop_price:.1f}")
    else:  # SELL
        if high >= stop_price:
            stop_hit = True
            recommendation = "SELL"
            alert_kind = "stop"
            reason_parts.append(f"intrabar high {high:.1f} pierced stop {stop_price:.1f}")
    
    # Check take profit
    target_hit = False
    if take_profit and not stop_hit:
        target_price = take_profit
        if action == "BUY":
            if high >= target_price:
                target_hit = True
                recommendation = "SELL"
                alert_kind = "target"
                reason_parts.append(f"intrabar high {high:.1f} reached target {target_price:.1f}")
        else:  # SELL
            if low <= target_price:
                target_hit = True
                recommendation = "SELL"
                alert_kind = "target"
                reason_parts.append(f"intrabar low {low:.1f} reached target {target_price:.1f}")
    
    # Check signal reversal (only if no stop/target hit)
    if not stop_hit and not target_hit and reversal:
        recommendation = "SELL"
        alert_kind = "reversal"
        reason_parts.append(f"signal reversal to {signal.action}")
    
    # Handle stale feed
    if is_stale and not stop_hit and not target_hit:
        # Stale feed - withhold mark, don't fabricate PnL
        recommendation = "STALE"
        alert_kind = "stale"
        reason_parts.append(f"HOLD cannot be confirmed (candle {candle_age:.0f} min old)")
        return PositionStatusResult(
            position={
                "symbol": _get_attr(position_row, "symbol"),
                "action": action,
                "entry_price": entry_price,
                "position_size": quantity,
                "position_value": entry_price * quantity,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
            },
            recommendation=recommendation,
            alert_kind=alert_kind,
            reason="; ".join(reason_parts),
            current_price=None,  # Withheld
            pnl=None,  # Withheld
            pnl_pct=None,  # Withheld
            stale=True,
            candle_age_minutes=candle_age,
            current_value=None,  # Withheld
        )
    
    # Stale but breach detected - still report the breach
    if is_stale and (stop_hit or target_hit):
        recommendation = "SELL"
        if alert_kind is None:
            alert_kind = "stop" if stop_hit else "target"
        reason_parts.append(f"{candle_age:.0f} min old")
    
    # Normal case
    if recommendation == "HOLD":
        reason_parts.append("no exit condition met")
    
    reason = "; ".join(reason_parts) if reason_parts else "no exit condition met"
    
    return PositionStatusResult(
        position={
            "symbol": _get_attr(position_row, "symbol"),
            "action": action,
            "entry_price": entry_price,
            "position_size": quantity,
            "position_value": entry_price * quantity,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
        },
        recommendation=recommendation,
        alert_kind=alert_kind,
        reason=reason,
        current_price=close,
        pnl=pnl,
        pnl_pct=pnl_pct,
        stale=is_stale,
        candle_age_minutes=candle_age,
        current_value=current_value,
    )


async def get_all_position_statuses(fetch_current_signals: bool = True, now: datetime | None = None) -> list[PositionStatusResult]:
    """Get status for all open positions."""
    if now is None:
        now = datetime.now(UTC)
    
    db = get_database()
    async with db.session() as session:
        repo = PositionRepository(session)
        open_positions = await repo.get_open()
    
    statuses = []
    for pos in open_positions:
        status = await get_position_status(pos, now=now, fetch_current_signals=fetch_current_signals)
        statuses.append(status)
    
    return statuses


async def _record_alert(session, position_id: int, symbol: str, kind: str, recommendation: str, candle_timestamp: datetime):
    """Record an alert to position_alerts table, avoiding duplicates."""
    # Check if we already recorded an alert for this position on this candle
    cache_key = (position_id, candle_timestamp.isoformat())
    if cache_key in _last_alert_cache:
        return False  # Already recorded
    
    alert = PositionAlert(
        position_id=position_id,
        symbol=symbol,
        kind=kind,
        recommendation=recommendation,
        candle_timestamp=candle_timestamp,
    )
    session.add(alert)
    await session.flush()
    _last_alert_cache[cache_key] = candle_timestamp
    return True


async def run_monitor_cycle(now: datetime | None = None) -> dict:
    """
    Run one monitor cycle: check all open positions, record alerts.
    
    Returns: {"checked": int, "alerts": int, "recorded": int}
    """
    if now is None:
        now = datetime.now(UTC)
    
    db = get_database()
    checked = 0
    alerts = 0
    recorded = 0

    async with db.session() as session:
        open_positions = await PositionRepository(session).get_open()
    # Evaluate outside the write session: candle reads can take seconds, and an open
    # write transaction meanwhile made order writes fail with "database is locked".
    flagged = []
    for pos in open_positions:
        checked += 1
        status = await get_position_status(pos, now=now, fetch_current_signals=True)
        if status.alert_kind is not None:
            alerts += 1
            flagged.append((pos, status))
    if flagged:
        async with db.session() as session:
            for pos, status in flagged:
                # Record alert if not already recorded for this candle
                recorded_flag = await _record_alert(
                    session, pos.id, pos.symbol,
                    status.alert_kind, status.recommendation, now
                )
                if recorded_flag:
                    recorded += 1

    return {"checked": checked, "alerts": alerts, "recorded": recorded}


def bracket_exit(bars, action: str, stop_loss: float, take_profit: float | None) -> tuple[float, str] | None:
    """The first stop or target fill in ``bars``, by the replay's rules (research/trade_outcomes).

    An opening gap through the stop fills at the open; a gap through the target
    fills at the target; a bar that touches both counts as the stop.
    """
    sign = 1 if action == "BUY" else -1

    def beyond_stop(price):
        return sign * (price - stop_loss) <= 0

    def beyond_target(price):
        return take_profit is not None and sign * (price - take_profit) >= 0

    for bar in bars.itertuples():
        o, high, low = float(bar.open), float(bar.high), float(bar.low)
        adverse, favourable = (low, high) if sign == 1 else (high, low)
        if beyond_stop(o):
            return o, "gap_stop"
        if beyond_target(o):
            return take_profit, "target"
        if beyond_stop(adverse):
            return stop_loss, "stop"
        if beyond_target(favourable):
            return take_profit, "target"
    return None


async def enforce_brackets(now: datetime | None = None) -> dict:
    """Paper mode: close each open position whose stop or target traded after entry.

    Behaves like a resting bracket order. Bars come from the live interval
    (``STORAGE_CANDLE_INTERVAL``) and only bars starting at or after the entry
    count, so the part of the entry bar after the fill is not checked. Never runs
    in live mode, where exits stay with the operator and the broker.
    """
    from fastapi import HTTPException
    from starlette.concurrency import run_in_threadpool

    from trading_agent.core.config import get_settings
    from trading_agent.execution.router import get_order_router
    from trading_agent.storage.parquet_store import get_parquet_store

    settings = get_settings()
    router = get_order_router()
    if router.mode != "paper" or not settings.paper.enforce_brackets:
        return {"checked": 0, "closed": []}
    async with get_database().session() as session:
        open_positions = await PositionRepository(session).get_open()
    closed = []
    for pos in open_positions:
        opened = pd.Timestamp(pos.opened_at)  # SQLite may hand back naive UTC or an ISO string
        opened = opened.tz_localize(UTC) if opened.tzinfo is None else opened.tz_convert(UTC)
        bars = await run_in_threadpool(get_parquet_store().read_candles, pos.symbol,
                                       settings.storage.candle_interval, start=opened)
        if bars.empty:
            continue
        hit = bracket_exit(bars, pos.action, pos.stop_loss, pos.take_profit)
        if hit is None:
            continue
        price, reason = hit
        try:
            result = await router.exit_position(pos.id, reason=f"paper_bracket_{reason}", price=price)
        except HTTPException as error:  # already closed elsewhere, or the order write failed
            logger.warning("paper_bracket_exit_failed", position_id=pos.id, symbol=pos.symbol,
                           reason=reason, detail=error.detail)
            continue
        logger.info("paper_bracket_exit", position_id=pos.id, symbol=pos.symbol, reason=reason,
                    level=price, exit_price=result["exit_price"], gross_pnl=result["gross_pnl"])
        closed.append({"position_id": pos.id, "symbol": pos.symbol, "reason": reason, **result})
    return {"checked": len(open_positions), "closed": closed}


_last_alert_cache: dict[tuple, datetime] = {}


def jsonable(obj: Any) -> dict:
    """Convert a PositionStatusResult or other object to JSON-serializable dict."""
    if hasattr(obj, '__dataclass_fields__'):
        # Dataclass - convert to dict
        result = {}
        for field_name in obj.__dataclass_fields__:
            value = getattr(obj, field_name)
            if isinstance(value, float) and (value != value or value == float('inf') or value == float('-inf')):
                value = None  # NaN/inf -> None
            result[field_name] = value
        return result
    elif isinstance(obj, dict):
        return obj
    else:
        # Try to convert to dict
        return {"value": str(obj)}


# Sync wrapper functions for testing
def get_all_position_statuses_sync(fetch_current_signals: bool = True, now: datetime | None = None) -> list[PositionStatusResult]:
    """Sync wrapper for get_all_position_statuses."""
    import asyncio
    return asyncio.run(get_all_position_statuses(fetch_current_signals=fetch_current_signals, now=now))


def get_position_status_sync(position_row: Any, now: datetime | None = None, fetch_current_signals: bool = True) -> PositionStatusResult:
    """Sync wrapper for get_position_status."""
    import asyncio
    return asyncio.run(get_position_status(position_row, now=now, fetch_current_signals=fetch_current_signals))


def run_monitor_cycle_sync(now: datetime | None = None) -> dict:
    """Sync wrapper for run_monitor_cycle."""
    import asyncio
    return asyncio.run(run_monitor_cycle(now=now))
