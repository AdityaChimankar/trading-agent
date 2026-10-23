"""Explicit, bounded strategy comparisons over locally stored candles."""

from threading import Lock
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from trading_agent.research.strategy_lab import INDEX_KEY, MAX_BARS, compare_profiles, read_completed
from trading_agent.research.trade_outcomes import TradePolicy
from trading_agent.storage.parquet_store import ParquetStore

router = APIRouter()
_lock = Lock()
Interval = Literal["minute", "5minute", "15minute"]


class CompareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=100)
    interval: Interval = "minute"
    max_bars: int = Field(default=5000, ge=250, le=10000)
    horizon: int = Field(default=24, ge=1, le=120)
    reward_risk: float = Field(default=2, ge=1, le=5, allow_inf_nan=False)
    cost_bps: float = Field(default=10, ge=0, le=100, allow_inf_nan=False)
    slippage_bps: float = Field(default=2, ge=0, le=50, allow_inf_nan=False)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        value = value.strip().upper()
        ParquetStore._validate_key(value)
        return value


class Metrics(BaseModel):
    trades: int
    sessions: int
    win_rate_pct: float | None
    mean_net_return_pct: float | None
    total_net_return_pct: float | None
    max_drawdown_pct_points: float | None
    profit_factor: float | None


class ProfileResult(BaseModel):
    id: str
    label: str
    description: str
    metrics: Metrics
    reference: Metrics
    sides: dict[Literal["BUY", "SELL"], Metrics]


class Windows(BaseModel):
    reference_start: str
    reference_end: str
    evaluation_start: str
    evaluation_end: str


class CompareResponse(BaseModel):
    version: Literal["strategy-lab-v1"]
    status: Literal["completed", "insufficient_history"]
    scope: Literal["exploratory_only"]
    promotion_eligible: Literal[False]
    symbol: str
    interval: Interval
    candles: int
    required_candles: int
    policy: TradePolicy
    windows: Windows | None
    profiles: list[ProfileResult]
    baselines: list[ProfileResult]
    skipped_profiles: list[dict] = []
    limitations: str


@router.post("/compare", response_model=CompareResponse)
def compare(request: CompareRequest):
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, "A strategy comparison is already running")
    try:
        frame = read_completed(request.symbol, request.interval, request.max_bars)
        index_frame = read_completed(INDEX_KEY, request.interval, MAX_BARS)
        policy = TradePolicy(horizon=request.horizon, reward_risk=request.reward_risk,
                             cost_bps=request.cost_bps, slippage_bps=request.slippage_bps)
        return compare_profiles(frame, request.symbol, request.interval, policy, index_frame)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    finally:
        _lock.release()
