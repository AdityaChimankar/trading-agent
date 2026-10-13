"""Pipeline router for running trading pipeline."""

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

router = APIRouter()


class PipelineStatus(BaseModel):
    status: str
    last_run: datetime | None
    next_run: datetime | None
    symbols_processed: int
    opportunities_found: int
    errors: List[str]


class PipelineRunRequest(BaseModel):
    symbols: List[str] | None = None
    interval: str = "5minute"
    force: bool = False


_pipeline_state = {
    "status": "idle",
    "last_run": None,
    "next_run": None,
    "symbols_processed": 0,
    "opportunities_found": 0,
    "errors": [],
}


@router.get("/status", response_model=PipelineStatus)
async def get_pipeline_status():
    """Get pipeline status."""
    return PipelineStatus(**_pipeline_state)


@router.post("/run")
async def run_pipeline(request: PipelineRunRequest, background_tasks: BackgroundTasks):
    """Run the trading pipeline."""
    if _pipeline_state["status"] == "running" and not request.force:
        raise HTTPException(status_code=409, detail="Pipeline already running")

    _pipeline_state["status"] = "running"
    _pipeline_state["errors"] = []

    # Run in background
    background_tasks.add_task(_execute_pipeline, request.symbols, request.interval)

    return {"status": "started", "message": "Pipeline execution started in background"}


async def _execute_pipeline(symbols: List[str] | None, interval: str):
    """Execute the pipeline."""
    try:
        from trading_agent.analysis.opportunity_finder import OpportunityFinder
        from trading_agent.strategy.candidates import CandidateGenerator
        from trading_agent.storage.parquet_store import get_parquet_store

        parquet = get_parquet_store()

        if symbols is None:
            symbols = parquet.get_symbols()

        if not symbols:
            _pipeline_state["status"] = "completed"
            _pipeline_state["last_run"] = datetime.now()
            return

        generator = CandidateGenerator()
        finder = OpportunityFinder()

        candidates = generator.generate_all(symbols, interval)
        opportunities = finder.find_opportunities(symbols, interval)

        _pipeline_state["symbols_processed"] = len(symbols)
        _pipeline_state["opportunities_found"] = len(opportunities)
        _pipeline_state["status"] = "completed"
        _pipeline_state["last_run"] = datetime.now()

    except Exception as e:
        _pipeline_state["status"] = "error"
        _pipeline_state["errors"].append(str(e))
        _pipeline_state["last_run"] = datetime.now()


@router.post("/schedule")
async def schedule_pipeline(cron: str = "*/5 9-15 * * 1-5"):
    """Schedule pipeline to run periodically."""
    # This would integrate with APScheduler
    return {"status": "scheduled", "cron": cron}
