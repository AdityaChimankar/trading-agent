"""Digest router."""

from fastapi import APIRouter, Query
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, date

router = APIRouter()


class DailyDigest(BaseModel):
    date: date
    market_summary: dict
    top_opportunities: list[dict]
    portfolio_summary: dict
    risk_metrics: dict
    news_sentiment: dict
    technical_highlights: list[dict]
    generated_at: datetime


@router.get("/daily", response_model=DailyDigest)
async def get_daily_digest(target_date: date | None = Query(None)):
    """Get daily trading digest."""
    from trading_agent.analysis.digest import DigestGenerator, generate_morning_briefing
    from trading_agent.storage.parquet_store import get_parquet_store

    parquet = get_parquet_store()
    symbols = parquet.get_symbols()

    generator = DigestGenerator(symbols=symbols)
    digest = generator.generate(target_date)

    return DailyDigest(
        date=digest.date,
        market_summary=digest.market_summary,
        top_opportunities=digest.top_opportunities,
        portfolio_summary=digest.portfolio_summary,
        risk_metrics=digest.risk_metrics,
        news_sentiment=digest.news_sentiment,
        technical_highlights=digest.technical_highlights,
        generated_at=digest.generated_at,
    )


@router.get("/daily/text")
async def get_daily_digest_text(target_date: date | None = Query(None)):
    """Get daily digest as formatted text."""
    from trading_agent.analysis.digest import DigestGenerator
    from trading_agent.storage.parquet_store import get_parquet_store

    parquet = get_parquet_store()
    symbols = parquet.get_symbols()

    generator = DigestGenerator(symbols=symbols)
    digest = generator.generate(target_date)

    return {"text": generator.format_digest(digest)}


@router.get("/morning")
async def get_morning_briefing():
    """Get morning pre-market briefing."""
    from trading_agent.analysis.digest import generate_morning_briefing
    from trading_agent.storage.parquet_store import get_parquet_store

    parquet = get_parquet_store()
    symbols = parquet.get_symbols()

    text = generate_morning_briefing(symbols)
    return {"text": text}
