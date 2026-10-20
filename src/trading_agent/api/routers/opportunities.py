"""Opportunities router."""

from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel
from typing import List, Literal
from datetime import datetime
from dataclasses import asdict

from trading_agent.analysis.opportunity_finder import OpportunityFinder, OpportunityConfig
from trading_agent.strategy.candidates import CandidateGenerator
from trading_agent.storage.parquet_store import get_parquet_store
from trading_agent.strategy.hybrid_strategy import HybridStrategy

router = APIRouter()
Interval = Literal["minute", "1minute", "5minute", "15minute", "day"]


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
def get_opportunities(
    symbols: str | None = Query(None, description="Comma-separated symbols"),
    interval: Interval = Query("5minute"),
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
def get_opportunity_detail(symbol: str, interval: Interval = Query("5minute")):
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
    details = finder.get_opportunity_details(opp, interval)

    return details


@router.post("/{symbol}/decision")
def preview_hybrid_decision(symbol: str, interval: Interval = Query("5minute")):
    """On-demand research preview; never submits orders or invokes external AI.

    The current API has no validated ML feature/model binding or async LLM
    adapter. Expose that explicitly instead of manufacturing additional votes.
    """
    symbol = symbol.strip().upper()
    parquet = get_parquet_store()
    if symbol not in parquet.get_symbols():
        raise HTTPException(status_code=404, detail="No stored data for symbol")
    candidate = CandidateGenerator(parquet_store=parquet).generate_for_symbol(symbol, interval)
    if candidate is None:
        raise HTTPException(
            status_code=422,
            detail="Not enough qualifying candles for this interval (history, volume, ADX or indicators).",
        )
    decision = HybridStrategy().decide(candidate)
    return {
        "symbol": symbol,
        "interval": interval,
        "timestamp": candidate.timestamp,
        "mode": "rule_only",
        "decision": asdict(decision),
        "limitations": [
            "Rule-only preview: ML model/feature binding is not connected to this endpoint.",
            "LLM inference is not connected; no external AI request was made.",
            "Voting share is not a probability of profit. Stored candles may be stale or incomplete.",
            "Research only: no order, paper position or portfolio approval is created.",
        ],
    }
