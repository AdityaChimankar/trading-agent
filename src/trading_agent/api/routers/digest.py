"""Digest router."""

from fastapi import APIRouter, Query
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, date

from starlette.concurrency import run_in_threadpool

router = APIRouter()
DIGEST_SYMBOLS = 30


class DailyDigest(BaseModel):
    date: date
    market_summary: dict
    top_opportunities: list[dict]
    portfolio_summary: dict
    risk_metrics: dict
    news_sentiment: dict
    technical_highlights: list[dict]
    generated_at: datetime


def _digest_symbols() -> list[str]:
    """The latest scan's strongest directional names, else the first stored symbols.

    The generator analyses every symbol it is given. Over all ~2,300 stored symbols
    one request ran for minutes on the API's event loop and froze every other
    endpoint (2026-10-26), so it is bounded like the classic dashboard's digest.
    """
    from trading_agent.analysis.research_jobs import get_research_jobs
    from trading_agent.storage.parquet_store import get_parquet_store

    rows = [row for row in get_research_jobs().snapshot(completed=True).get("rows", [])
            if row.get("action") in {"BUY", "SELL"}]
    if rows:
        return [row["symbol"] for row in sorted(rows, key=lambda row: -row["score"])[:DIGEST_SYMBOLS]]
    return get_parquet_store().get_symbols()[:DIGEST_SYMBOLS]


def _daily(target_date: date | None):
    from trading_agent.analysis.digest import DigestGenerator

    generator = DigestGenerator(symbols=_digest_symbols())
    return generator, generator.generate(target_date)


@router.get("/daily", response_model=DailyDigest)
async def get_daily_digest(target_date: date | None = Query(None)):
    """Get daily trading digest."""
    _, digest = await run_in_threadpool(_daily, target_date)

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
    generator, digest = await run_in_threadpool(_daily, target_date)

    return {"text": generator.format_digest(digest)}


@router.get("/morning")
async def get_morning_briefing():
    """Get morning pre-market briefing."""
    from trading_agent.analysis.digest import generate_morning_briefing

    text = await run_in_threadpool(lambda: generate_morning_briefing(_digest_symbols()))
    return {"text": text}
