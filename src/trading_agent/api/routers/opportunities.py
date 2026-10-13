"""Opportunities router."""

from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

from trading_agent.analysis.opportunity_finder import OpportunityFinder, OpportunityConfig
from trading_agent.strategy.candidates import CandidateGenerator
from trading_agent.storage.parquet_store import get_parquet_store

router = APIRouter()


class OpportunityResponse(BaseModel):
    symbol: str
    timestamp: datetime
    score: float
    rank: int
    rule_signal: str
    rule_confidence: float
    ml_signal: str | None
    ml_confidence: float
    llm_signal: str | None
    llm_confidence: float
    risk_reward: float
    atr: float
    volume: int
    price: float


@router.get("/", response_model=List[OpportunityResponse])
async def get_opportunities(
    symbols: str | None = Query(None, description="Comma-separated symbols"),
    interval: str = Query("5minute"),
    min_score: float = Query(0.3, ge=0, le=1),
    max_results: int = Query(20, ge=1, le=100),
):
    """Get trading opportunities."""
    # Parse symbols
    if symbols:
        symbol_list = [s.strip().upper() for s in symbols.split(",")]
    else:
        # Default to all symbols with data
        parquet = get_parquet_store()
        symbol_list = parquet.get_symbols()

    if not symbol_list:
        return []

    # Configure finder
    config = OpportunityConfig(
        min_score=min_score,
        max_opportunities=max_results,
    )
    finder = OpportunityFinder(config=config)

    # Generate opportunities
    opportunities = finder.find_opportunities(symbol_list, interval)

    return [
        OpportunityResponse(
            symbol=opp.symbol,
            timestamp=opp.timestamp,
            score=opp.score,
            rank=opp.rank,
            rule_signal=opp.rule_signal,
            rule_confidence=opp.rule_confidence,
            ml_signal=opp.ml_signal,
            ml_confidence=opp.ml_confidence,
            llm_signal=opp.llm_signal,
            llm_confidence=opp.llm_confidence,
            risk_reward=opp.risk_reward,
            atr=opp.atr,
            volume=opp.volume,
            price=opp.price,
        )
        for opp in opportunities
    ]


@router.get("/{symbol}")
async def get_opportunity_detail(symbol: str, interval: str = Query("5minute")):
    """Get detailed opportunity analysis for a symbol."""
    symbol = symbol.upper()
    parquet = get_parquet_store()

    if symbol not in parquet.get_symbols():
        raise HTTPException(status_code=404, detail="No data for symbol")

    finder = OpportunityFinder()
    opportunities = finder.find_opportunities([symbol], interval)

    if not opportunities:
        return {"symbol": symbol, "opportunity": None}

    opp = opportunities[0]
    details = finder.get_opportunity_details(opp)

    return details
