"""Order router: one path for paper and live entries/exits.

Entry order of operations (the part that matters with real money):

1. Session controls (kill switch, cutoff, daily trade count) are checked.
2. The position is reserved through the same risk-checked ledger path manual
   paper entries use, under the SQLite write lock, at the worse of the
   reference price and a fresh stored price (live entries require one).
   A risk rejection therefore happens BEFORE anything reaches a broker.
3. The order is sent. A fill rewrites the reserved position at the actual
   fill price; a rejection removes the reservation.

Every order is persisted with its lifecycle (PENDING -> SUBMITTED -> FILLED /
REJECTED), modelled charges and slippage, and audited.

Live orders require EXECUTION_MODE=live AND EXECUTION_LIVE_CONFIRMATION set to
LIVE_CONFIRMATION exactly; anything else routes to the paper broker.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import delete, select
from starlette.concurrency import run_in_threadpool

from trading_agent.core.config import get_settings
from trading_agent.core.logging_config import get_logger
from trading_agent.execution.costs import order_charges, slipped_price
from trading_agent.risk.controls import audit
from trading_agent.storage.db import Order, Position, get_database

logger = get_logger(__name__)
LIVE_CONFIRMATION = "I-ACCEPT-REAL-MONEY-RISK"


@dataclass(frozen=True)
class Fill:
    price: float
    broker_order_id: str | None


class BrokerRejected(Exception):
    pass


class PaperBroker:
    """Fills immediately at the reference price moved against you by slippage."""

    mode = "paper"

    def __init__(self, slippage_bps: float):
        self.slippage_bps = slippage_bps

    async def execute(self, order: Order) -> Fill:
        return Fill(slipped_price(order.side, order.reference_price, self.slippage_bps), None)


class KiteBroker:
    """MARKET orders through Kite Connect, confirmed via order history."""

    mode = "live"

    def __init__(self, kite=None, poll_seconds: float = 0.5, timeout_seconds: float = 15.0):
        from trading_agent.ingest.kite_auth import get_kite_client

        self.kite = kite or get_kite_client()
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds
        self.slippage_bps = 0.0  # real fills carry their own slippage

    async def execute(self, order: Order) -> Fill:
        kite = self.kite
        try:
            broker_id = await run_in_threadpool(
                kite.place_order, variety="regular", exchange="NSE", tradingsymbol=order.symbol,
                transaction_type=order.side, quantity=order.quantity, product=order.product,
                order_type="MARKET", tag=order.client_order_id[:20],
            )
        except Exception as error:
            raise BrokerRejected(f"Order placement failed: {error}") from error
        waited = 0.0
        while waited < self.timeout_seconds:
            history = await run_in_threadpool(kite.order_history, broker_id)
            state = history[-1] if history else {}
            status = str(state.get("status", "")).upper()
            if status == "COMPLETE":
                return Fill(float(state["average_price"]), str(broker_id))
            if status in {"REJECTED", "CANCELLED"}:
                raise BrokerRejected(state.get("status_message") or status)
            await asyncio.sleep(self.poll_seconds)
            waited += self.poll_seconds
        # Unknown outcome: never assume either way. Reconciliation must resolve it.
        raise BrokerRejected(f"No fill confirmation within {self.timeout_seconds}s for {broker_id}; reconcile")


def live_enabled() -> bool:
    execution = get_settings().execution
    return execution.mode == "live" and execution.live_confirmation == LIVE_CONFIRMATION


class OrderRouter:
    def __init__(self, broker=None):
        execution = get_settings().execution
        self.broker = broker or (KiteBroker() if live_enabled() else PaperBroker(execution.slippage_bps))

    @property
    def mode(self) -> str:
        return self.broker.mode

    async def _new_order(self, **fields) -> Order:
        execution = get_settings().execution
        async with get_database().session() as session:
            order = Order(client_order_id=uuid4().hex, mode=self.mode, product=execution.product,
                          status="PENDING", slippage_bps=getattr(self.broker, "slippage_bps", 0.0), **fields)
            session.add(order)
        return order

    async def _update(self, order_id: int, **fields) -> Order:
        async with get_database().session() as session:
            order = await session.get(Order, order_id)
            for key, value in fields.items():
                setattr(order, key, value)
        return order

    async def _send(self, order: Order) -> Fill:
        await self._update(order.id, status="SUBMITTED")
        try:
            fill = await self.broker.execute(order)
        except BrokerRejected as error:
            await self._update(order.id, status="REJECTED", error=str(error))
            audit("order_rejected", order_id=order.id, symbol=order.symbol, side=order.side, error=str(error))
            raise
        charges = order_charges(order.side, fill.price, order.quantity)["total"]
        await self._update(order.id, status="FILLED", fill_price=fill.price,
                           broker_order_id=fill.broker_order_id, charges=charges)
        audit("order_filled", order_id=order.id, symbol=order.symbol, side=order.side,
              quantity=order.quantity, fill_price=fill.price, charges=charges, mode=self.mode)
        return fill

    async def enter(self, *, symbol: str, side: str, quantity: int, reference_price: float,
                    stop_loss: float, take_profit: float | None, strategy: str = "router",
                    reason: str | None = None) -> dict:
        from trading_agent.api.routers.positions import PositionCreate, create_position

        # A plan priced earlier must not understate risk after the market moved:
        # measure from the worse of the plan price and a fresh stored price.
        fresh = await fresh_price(symbol)
        if fresh is None and self.mode == "live":
            raise HTTPException(409, (
                f"No stored {symbol} price in the last {get_settings().execution.max_quote_age_seconds}s; "
                "live entries need a fresh price"))
        risk_price = reference_price
        if fresh is not None:
            direction = 1 if side == "BUY" else -1
            if direction * (fresh - stop_loss) <= 0:
                raise HTTPException(409, f"{symbol} last traded {fresh:g}, at or through the stop {stop_loss:g}; re-plan")
            risk_price = max(reference_price, fresh) if direction > 0 else min(reference_price, fresh)
            if take_profit is not None and direction * (take_profit - risk_price) <= 0:
                raise HTTPException(409, f"{symbol} price {risk_price:g} is at or past the target {take_profit:g}; re-plan")
            reference_price = fresh  # a market order fills at the market, not at the plan price

        await self._check_participation(symbol, quantity, risk_price)
        # Step 2: reserve through the risk-checked ledger path (raises 409 on any limit).
        reserved = await create_position(PositionCreate(
            symbol=symbol, action=side, entry_price=risk_price, quantity=quantity,
            stop_loss=stop_loss, take_profit=take_profit, strategy=strategy,
        ))
        order = await self._new_order(position_id=reserved.id, symbol=symbol, side=side, quantity=quantity,
                                      purpose="entry", reference_price=reference_price, reason=reason)
        try:
            fill = await self._send(order)
        except BrokerRejected as error:
            async with get_database().session() as session:
                await session.execute(delete(Position).where(Position.id == reserved.id))
            raise HTTPException(502, f"Entry not filled; reservation released: {error}") from error
        async with get_database().session() as session:
            position = await session.get(Position, reserved.id)
            position.entry_price = fill.price
            position.position_value = fill.price * quantity
            position.risk_amount = abs(fill.price - stop_loss) * quantity
        return {"position_id": reserved.id, "order_id": order.id, "fill_price": fill.price,
                "quantity": quantity, "mode": self.mode}

    async def _check_participation(self, symbol: str, quantity: int, price: float) -> None:
        """Cap size at a share of average daily traded value (volume_insights)."""
        from trading_agent.analysis.volume_insights import symbol_volume

        try:
            profile = await run_in_threadpool(symbol_volume, symbol)
        except Exception:
            profile = None
        if profile is None:
            if self.mode == "live":
                raise HTTPException(409, "Cannot verify liquidity for a live order (no volume history)")
            return
        if profile["liquidity"] == "illiquid":
            raise HTTPException(409, f"{symbol} is illiquid: {profile['reason']}")
        if quantity * price > profile["max_position_value"]:
            raise HTTPException(409, (
                f"Size INR {quantity * price:,.0f} exceeds {profile['participation_pct']:.2f}% of "
                f"{symbol}'s average daily traded value (max {profile['max_quantity']} shares)"))
        remaining = profile.get("max_quantity_remaining")
        if remaining is not None and quantity > remaining:
            raise HTTPException(409, (
                f"{quantity} shares exceeds {profile['remaining_participation_pct']:.1f}% of {symbol}'s "
                f"forecast remaining volume today (max {remaining} shares)"))

    async def exit_position(self, position_id: int, *, reason: str, price: float | None = None) -> dict:
        from trading_agent.api.routers.positions import close_position

        async with get_database().session() as session:
            position = await session.get(Position, position_id)
        if position is None or position.status.lower() != "open":
            raise HTTPException(404, "Open position not found")
        reference = price if price is not None else await latest_price(position.symbol)
        side = "SELL" if position.action == "BUY" else "BUY"
        order = await self._new_order(position_id=position_id, symbol=position.symbol, side=side,
                                      quantity=position.quantity, purpose="exit",
                                      reference_price=reference, reason=reason)
        try:
            fill = await self._send(order)
        except BrokerRejected as error:
            raise HTTPException(502, f"Exit not filled; position still open: {error}") from error
        closed = await close_position(position_id, exit_price=fill.price)
        return {"order_id": order.id, "exit_price": fill.price, "gross_pnl": round(closed["pnl"], 2),
                "mode": self.mode}

    async def list_orders(self, limit: int = 200) -> list[Order]:
        async with get_database().session() as session:
            result = await session.execute(select(Order).order_by(Order.id.desc()).limit(limit))
            return list(result.scalars())


async def latest_price(symbol: str) -> float:
    from trading_agent.storage.parquet_store import get_parquet_store

    frame = await run_in_threadpool(get_parquet_store().read_latest, symbol,
                                    get_settings().storage.candle_interval, 1)
    if frame.empty:
        raise HTTPException(409, f"No stored price for {symbol}")
    return float(frame["close"].iloc[-1])


BAR_SECONDS = {"minute": 60, "1minute": 60, "5minute": 300, "15minute": 900}


async def fresh_price(symbol: str) -> float | None:
    """Close of the most recently ended stored bar, 1-minute or the live interval.

    None when that bar ended more than EXECUTION_MAX_QUOTE_AGE_SECONDS ago. The live
    ticker's 5-minute bar can be minutes behind backfilled 1-minute bars, so the bar
    that ended last wins, never a fixed interval.
    """
    from trading_agent.storage.parquet_store import get_parquet_store

    settings = get_settings()
    newest = None
    for interval in dict.fromkeys(("minute", settings.storage.candle_interval)):
        try:
            frame = await run_in_threadpool(get_parquet_store().read_latest, symbol, interval, 1)
        except Exception:
            continue
        if frame.empty:
            continue
        ended = frame["timestamp"].iloc[-1].to_pydatetime() + timedelta(seconds=BAR_SECONDS.get(interval, 60))
        if newest is None or ended > newest[0]:
            newest = (ended, float(frame["close"].iloc[-1]))
    if newest is None or datetime.now(UTC) - newest[0] > timedelta(seconds=settings.execution.max_quote_age_seconds):
        return None
    return newest[1]


def get_order_router() -> OrderRouter:
    # Built per call so EXECUTION_* changes and the kill switch apply immediately.
    return OrderRouter()
