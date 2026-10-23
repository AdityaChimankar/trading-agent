"""Reconcile the local book against orders and, when live, the broker.

Checks, each reported as an issue rather than silently corrected:

- Unconfirmed orders: PENDING/SUBMITTED longer than ``stale_after`` (a live
  order whose fill confirmation never arrived must be resolved by hand).
- Router positions whose filled entry order is missing or has a different
  quantity.
- Live only: per-symbol net quantity in the local book vs Zerodha's MIS
  net positions.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from trading_agent.execution.router import live_enabled
from trading_agent.storage.db import Order, Position, get_database


def _aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def compare_net_quantities(local: dict[str, int], broker: dict[str, int]) -> list[dict]:
    issues = []
    for symbol in sorted(set(local) | set(broker)):
        if local.get(symbol, 0) != broker.get(symbol, 0):
            issues.append({"kind": "broker_quantity_mismatch", "symbol": symbol,
                           "local_quantity": local.get(symbol, 0), "broker_quantity": broker.get(symbol, 0)})
    return issues


async def reconcile(broker_positions: list[dict] | None = None, stale_after: timedelta = timedelta(minutes=2)) -> dict:
    now = datetime.now(UTC)
    async with get_database().session() as session:
        open_positions = list((await session.execute(
            select(Position).where(Position.status.in_(["open", "OPEN"])))).scalars())
        orders = list((await session.execute(select(Order))).scalars())

    issues = []
    for order in orders:
        if order.status in {"PENDING", "SUBMITTED"} and now - _aware(order.created_at) > stale_after:
            issues.append({"kind": "unconfirmed_order", "order_id": order.id, "symbol": order.symbol,
                           "status": order.status, "broker_order_id": order.broker_order_id})

    entries = defaultdict(list)
    for order in orders:
        if order.purpose == "entry" and order.status == "FILLED":
            entries[order.position_id].append(order)
    local_net: dict[str, int] = defaultdict(int)
    for position in open_positions:
        sign = 1 if position.action == "BUY" else -1
        local_net[position.symbol] += sign * position.quantity
        if position.strategy in {"router", "hybrid_scan"}:
            filled = sum(o.quantity for o in entries.get(position.id, []))
            if filled != position.quantity:
                issues.append({"kind": "entry_fill_mismatch", "position_id": position.id,
                               "symbol": position.symbol, "position_quantity": position.quantity,
                               "filled_quantity": filled})

    broker_checked = False
    if live_enabled() or broker_positions is not None:
        if broker_positions is None:
            from trading_agent.ingest.kite_auth import get_kite_client

            kite = get_kite_client()
            broker_positions = (await run_in_threadpool(kite.positions)).get("net", [])
        broker_net: dict[str, int] = defaultdict(int)
        for row in broker_positions:
            if row.get("product", "MIS") == "MIS" and row.get("exchange", "NSE") == "NSE":
                broker_net[row["tradingsymbol"]] += int(row.get("quantity", 0))
        issues += compare_net_quantities(
            {k: v for k, v in local_net.items() if v}, {k: v for k, v in broker_net.items() if v})
        broker_checked = True

    return {"checked_at": now.isoformat(), "ok": not issues, "issues": issues,
            "open_positions": len(open_positions), "orders": len(orders), "broker_checked": broker_checked}
