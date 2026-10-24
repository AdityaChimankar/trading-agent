"""Risk controls, order execution and analytics endpoints (/api/v1)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from trading_agent.api.serializers import jsonable
from trading_agent.storage.db import Position, get_database

router = APIRouter()


async def _ledger_rows() -> list[dict]:
    async with get_database().session() as session:
        rows = (await session.execute(select(Position))).scalars()
        return [{"opened_at": p.opened_at, "status": p.status} for p in rows]


# --- controls ----------------------------------------------------------------------

class HaltRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=200)


@router.get("/controls/status")
async def controls_status():
    from trading_agent.core.config import get_settings
    from trading_agent.execution.router import live_enabled
    from trading_agent.risk.controls import session_status
    from trading_agent.risk.ledger_guard import read_ledger_state

    status = session_status(await _ledger_rows())
    try:
        ledger = await run_in_threadpool(read_ledger_state)
        status["ledger"] = {k: ledger[k] for k in ("entries_allowed", "block_reason", "realized_equity",
                                                   "daily_realized_pnl", "realized_drawdown_pct")}
    except Exception:
        status["ledger"] = {"entries_allowed": False, "block_reason": "Ledger cannot be verified"}
    limits = get_settings().paper
    status["limits"] = limits.model_dump()
    status["execution_mode"] = "live" if live_enabled() else "paper"
    return jsonable(status)


@router.post("/controls/halt")
async def halt(payload: HaltRequest):
    from trading_agent.risk.controls import engage_kill_switch

    return engage_kill_switch(payload.reason)


@router.post("/controls/resume")
async def resume():
    from trading_agent.risk.controls import release_kill_switch

    return release_kill_switch()


@router.post("/controls/square-off")
async def square_off():
    from trading_agent.risk.controls import square_off_all

    return {"results": await square_off_all(reason="manual_square_off")}


@router.get("/controls/audit")
def audit_log(day: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"), limit: int = Query(200, ge=1, le=2000)):
    from trading_agent.risk.controls import read_audit

    return read_audit(day, limit)


# --- execution ----------------------------------------------------------------------

class EntryRequest(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")
    symbol: str = Field(min_length=1, max_length=20, pattern=r"^[A-Za-z0-9&_.-]+$")
    side: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0, strict=True)
    reference_price: float = Field(gt=0)
    stop_loss: float = Field(gt=0)
    take_profit: float | None = Field(default=None, gt=0)
    reason: str | None = Field(default=None, max_length=200)

    @field_validator("symbol", "side", mode="before")
    @classmethod
    def upper(cls, value):
        return value.strip().upper() if isinstance(value, str) else value


@router.get("/execution/orders")
async def orders(limit: int = Query(200, ge=1, le=2000)):
    from trading_agent.execution.router import get_order_router

    from datetime import UTC, datetime

    def value(order, name):
        raw = getattr(order, name)
        # SQLite returns naive UTC; send explicit offsets so browsers don't read local time.
        return raw.replace(tzinfo=UTC) if isinstance(raw, datetime) and raw.tzinfo is None else raw

    rows = await get_order_router().list_orders(limit)
    return jsonable([{c.name: value(o, c.name) for c in o.__table__.columns} for o in rows])


@router.post("/execution/enter")
async def enter(payload: EntryRequest):
    from trading_agent.execution.router import get_order_router

    return await get_order_router().enter(
        symbol=payload.symbol, side=payload.side, quantity=payload.quantity,
        reference_price=payload.reference_price, stop_loss=payload.stop_loss,
        take_profit=payload.take_profit, strategy="router", reason=payload.reason)


@router.post("/execution/exit/{position_id}")
async def exit_position(position_id: int, price: float | None = Query(None, gt=0, allow_inf_nan=False)):
    from trading_agent.execution.router import get_order_router

    return await get_order_router().exit_position(position_id, reason="manual_exit", price=price)


@router.get("/execution/reconcile")
async def reconcile():
    from trading_agent.execution.reconcile import reconcile as run

    return jsonable(await run())


@router.get("/execution/costs")
def cost_preview(price: float = Query(..., gt=0), quantity: int = Query(..., gt=0),
                 side: Literal["BUY", "SELL"] = "BUY"):
    from trading_agent.execution.costs import breakeven_move_pct, order_charges, rates_dict, round_trip_charges

    return {"entry": order_charges(side, price, quantity),
            "round_trip_at_same_price": round_trip_charges(side, price, price, quantity),
            "breakeven_move_pct": round(breakeven_move_pct(price, quantity), 4), "rates": rates_dict()}


# --- analytics ------------------------------------------------------------------------

@router.get("/analytics/performance")
async def performance():
    from trading_agent.analysis.performance import performance_report

    return jsonable(await performance_report())


@router.get("/analytics/volume")
async def volume_board(top: int = Query(40, ge=1, le=200)):
    from trading_agent.analysis.research_jobs import get_research_jobs
    from trading_agent.analysis.volume_insights import scan_volume

    rows = (await run_in_threadpool(get_research_jobs().snapshot)).get("rows", [])
    return jsonable(await run_in_threadpool(scan_volume, rows, top))


@router.get("/analytics/volume/{symbol}")
async def volume_symbol(symbol: str):
    from fastapi import HTTPException

    from trading_agent.analysis.research_news import news_path
    from trading_agent.analysis.volume_insights import symbol_volume

    symbol = symbol.strip().upper()
    try:
        news_path(symbol)
    except ValueError as error:
        raise HTTPException(422, "Invalid symbol") from error
    profile = await run_in_threadpool(symbol_volume, symbol)
    if profile is None:
        raise HTTPException(404, "Not enough stored minute history for a volume profile")
    return jsonable(profile)


@router.get("/analytics/drift")
async def drift():
    from trading_agent.strategy.model_monitoring import drift_report, fallback_state

    return jsonable({"report": await run_in_threadpool(drift_report), "fallback": fallback_state()})


@router.post("/analytics/drift/evaluate")
async def drift_evaluate():
    from trading_agent.strategy.model_monitoring import evaluate_and_apply

    return jsonable(await run_in_threadpool(evaluate_and_apply))


@router.post("/analytics/drift/clear")
def drift_clear():
    from trading_agent.strategy.model_monitoring import clear_fallback

    return clear_fallback()


@router.get("/analytics/volume/{symbol}/forecast-check")
async def volume_forecast_check(symbol: str, interval: Literal["minute", "5minute"] = "minute"):
    """Out-of-sample error of the U-shape volume forecast against flat-average and pace baselines."""
    from datetime import UTC, datetime

    from fastapi import HTTPException

    from trading_agent.analysis.research_engine import completed_frame
    from trading_agent.analysis.research_news import news_path
    from trading_agent.analysis.volume_insights import evaluate_volume_forecast
    from trading_agent.storage.parquet_store import get_parquet_store

    symbol = symbol.strip().upper()
    try:
        news_path(symbol)
    except ValueError as error:
        raise HTTPException(422, "Invalid symbol") from error

    def check():
        frame = completed_frame(get_parquet_store(), symbol, interval, datetime.now(UTC))
        return evaluate_volume_forecast(frame)

    return jsonable({"symbol": symbol, "interval": interval, **await run_in_threadpool(check)})


class RankingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interval: Literal["minute", "5minute", "15minute"] = "5minute"
    max_lag: int = Field(default=10, ge=1, le=240)
    train_days: int = Field(default=60, ge=20, le=1000)
    retrain_days: int = Field(default=20, ge=1, le=250)
    top_k: int = Field(default=10, ge=1, le=50)
    cost_bps: float = Field(default=15.0, ge=0, le=200, allow_inf_nan=False)
    min_value_cr: float = Field(default=10.0, ge=0, le=10000, allow_inf_nan=False)


@router.get("/analytics/cross-sectional")
def cross_sectional_status():
    from trading_agent.research.cross_sectional import get_job, read_report

    return jsonable({"job": get_job().state, "report": read_report()})


@router.post("/analytics/cross-sectional/run", status_code=202)
def cross_sectional_run(payload: RankingRequest | None = None):
    from fastapi import HTTPException

    from trading_agent.research.cross_sectional import RankConfig, get_job

    payload = payload or RankingRequest()
    config = RankConfig(max_lag=payload.max_lag, train_days=payload.train_days,
                        retrain_days=payload.retrain_days, top_k=payload.top_k, cost_bps=payload.cost_bps,
                        min_avg_value_cr=payload.min_value_cr)
    if not get_job().start(interval=payload.interval, config=config):
        raise HTTPException(409, "A cross-sectional evaluation is already running")
    return {"started": True}
