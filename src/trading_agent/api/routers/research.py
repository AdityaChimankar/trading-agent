"""Restored dashboard contracts: explicit scan, symbol chart and news actions."""
from typing import Literal
from threading import Lock

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from trading_agent.analysis.research_engine import analyze_symbol, chart_payload, universe
from trading_agent.analysis.research_jobs import get_research_jobs
from trading_agent.analysis.research_news import read_news, refresh_news
from trading_agent.api.serializers import jsonable
from trading_agent.core.config import get_settings

router = APIRouter()
Interval = Literal["minute", "5minute", "15minute", "day"]
Mode = Literal["rule_ml_combined", "rule_only", "ml_only"]
_optimizer_lock = Lock()
_validation_lock = Lock()


class OutcomeRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=100)
    interval: Literal["minute", "5minute", "15minute"] = "minute"
    reward_risk: float = Field(default=2, ge=1, le=5, allow_inf_nan=False)
    cost_bps: float = Field(default=10, ge=0, le=100, allow_inf_nan=False)
    slippage_bps: float = Field(default=2, ge=0, le=50, allow_inf_nan=False)
    horizon: int = Field(default=24, ge=1, le=120)

    @field_validator("symbol")
    @classmethod
    def clean_symbol(cls, value):
        from trading_agent.storage.parquet_store import ParquetStore
        value = value.strip().upper()
        ParquetStore._validate_key(value)
        return value


@router.post("/outcome-validation")
def validate_outcomes(request: OutcomeRequest):
    from trading_agent.analysis.outcome_validation import validate_symbol
    if not _validation_lock.acquire(blocking=False):
        raise HTTPException(409, "An outcome validation is already running")
    try:
        return validate_symbol(**request.model_dump())
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    finally:
        _validation_lock.release()


class WalkForwardRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=100)
    interval: Interval = "minute"
    train_candles: int = Field(default=3750, ge=250, le=10000)
    test_candles: int = Field(default=750, ge=100, le=5000)
    cost_bps: float = Field(default=5, ge=0, le=100, allow_inf_nan=False)

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value):
        from trading_agent.storage.parquet_store import ParquetStore
        value = value.strip().upper()
        ParquetStore._validate_key(value)
        return value


@router.post("/walk-forward")
def walk_forward(request: WalkForwardRequest):
    # FastAPI runs this bounded, single-symbol calculation off the event loop.
    from trading_agent.research.walk_forward_optimizer import run_walk_forward
    if not _optimizer_lock.acquire(blocking=False):
        raise HTTPException(409, "A walk-forward evaluation is already running; try again later")
    try:
        return jsonable(run_walk_forward(verbose=False, **request.model_dump()))
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    finally:
        _optimizer_lock.release()


class ScanRequest(BaseModel):
    symbols: list[str] | None = Field(default=None, min_length=1, max_length=5000)
    interval: Interval = "minute"
    mode: Mode = "rule_ml_combined"
    capital: float = Field(default=100000, gt=0, le=1e10, allow_inf_nan=False)
    risk_pct: float = Field(default=1, gt=0, le=2, allow_inf_nan=False)
    reward_risk: float = Field(default=2, ge=1, le=5, allow_inf_nan=False)
    require_validated: bool = True

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, value):
        from trading_agent.storage.parquet_store import ParquetStore
        if value is not None:
            value = [symbol.strip().upper() for symbol in value]
            for symbol in value:
                ParquetStore._validate_key(symbol)
        return value


class AutomaticScanRequest(ScanRequest):
    """Automatic runs must retain the full-universe evidence policy."""

    symbols: None = None
    interval: Literal["minute", "5minute", "15minute"] = "minute"
    require_validated: Literal[True] = True


class AutomationRequest(BaseModel):
    scan: AutomaticScanRequest
    min_scan_seconds: int = Field(default=120, ge=60, le=3600)


@router.get("/readiness")
def research_readiness(interval: Interval = "minute"):
    from trading_agent.analysis.readiness import readiness
    return jsonable(readiness(interval))


@router.get("/automation")
def automation_status():
    from trading_agent.analysis.research_automation import get_research_automation
    return get_research_automation().snapshot()


@router.post("/automation/enable")
def enable_automation(request: AutomationRequest):
    from trading_agent.analysis.research_automation import get_research_automation
    worker = get_research_automation()
    if get_settings().api.workers != 1 or not worker.snapshot()["running"]:
        raise HTTPException(409, "Automatic research requires one running API worker")
    try:
        return worker.enable(request.scan.model_dump(), request.min_scan_seconds)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error


@router.post("/automation/pause")
def pause_automation():
    from trading_agent.analysis.research_automation import get_research_automation
    return get_research_automation().pause()


@router.get("/universe")
def get_universe():
    symbols = universe()
    return {"symbols": symbols, "count": len(symbols), "llm_provider": get_settings().llm.provider}


@router.get("/outcome-models")
def outcome_models():
    from trading_agent.strategy.outcome_model import candidate_status
    return candidate_status()


@router.post("/scan", status_code=202)
def start_scan(request: ScanRequest):
    if get_settings().api.workers != 1:
        raise HTTPException(409, "Research scans require API_WORKERS=1")
    try:
        return get_research_jobs().start(request.model_dump())
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error


@router.get("/scan")
def scan_status():
    return jsonable(get_research_jobs().snapshot())


@router.post("/scan/cancel")
def cancel_scan():
    from trading_agent.analysis.research_automation import get_research_automation
    get_research_automation().pause("Scan cancelled. Enable automatic research again explicitly.")
    get_research_jobs().cancelled.set()
    return {"status": "cancellation_requested"}


@router.get("/symbols/{symbol}")
def symbol_research(symbol: str, interval: Interval = "minute", mode: Mode = "rule_ml_combined"):
    symbol = symbol.strip().upper()
    try:
        row, df = analyze_symbol(symbol, interval, mode)
        return jsonable({"analysis": row, "chart": chart_payload(symbol, df), "news": read_news(symbol)})
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.get("/symbols/{symbol}/news")
def cached_news(symbol: str):
    try:
        return read_news(symbol.strip().upper())
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.post("/symbols/{symbol}/news")
async def refresh_symbol_news(symbol: str, use_llm: bool = False):
    try:
        return await refresh_news(symbol.strip().upper(), use_llm)
    except ValueError as error:
        raise HTTPException(422, "Invalid symbol or sentiment response") from error
    except Exception as error:
        raise HTTPException(502, "News or LLM provider unavailable; no fabricated sentiment was saved") from error
