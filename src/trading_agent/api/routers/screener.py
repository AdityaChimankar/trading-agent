"""Market screener endpoints (/api/v1/screener): sectors, trading screens, intraday filters.

GETs read the saved scan snapshot; only ``POST /run`` starts a scan.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from trading_agent.analysis.screener import (
    NEAR_52W_PCT,
    PRESETS,
    SORT_FIELDS,
    filter_rows,
    screen_counts,
    sector_breadth,
)
from trading_agent.analysis.screener_jobs import get_screener_jobs, technicals_check
from trading_agent.api.serializers import jsonable
from trading_agent.core.config import get_settings

router = APIRouter()
Window = Literal["5m", "15m", "1h", "1d"]
Level = Literal["r1", "r2", "r3"]


class RunRequest(BaseModel):
    universe: Literal["all", "indexed"] = "all"
    quotes: bool = True  # live Kite quotes over the stored candles (about 5 calls for 2,300 symbols)


def _meta(state: dict) -> dict:
    return {key: state.get(key) for key in ("status", "processed", "total", "started_at", "completed_at",
                                            "latest_session", "universe", "sector_map_loaded", "quotes")}


@router.post("/run", status_code=202)
def run_scan(request: RunRequest | None = None):
    if get_settings().api.workers != 1:
        raise HTTPException(409, "Screener scans require API_WORKERS=1")
    try:
        request = request or RunRequest()
        return jsonable(get_screener_jobs().start(request.universe, quotes=request.quotes))
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error


@router.post("/cancel")
def cancel_scan():
    get_screener_jobs().cancelled.set()
    return {"status": "cancellation_requested"}


@router.get("/status")
def scan_status():
    state = get_screener_jobs().snapshot(rows=False)
    return jsonable({**_meta(state), "skipped": state.get("skipped", 0), "errors": len(state.get("errors", [])),
                     "check": state.get("check")})


@router.get("/check")
def check_technicals():
    """Data-quality read of the saved technicals, recomputed against the current time."""
    return jsonable(technicals_check(get_screener_jobs().rows()))


@router.get("/screens")
def screens():
    return jsonable(screen_counts(get_screener_jobs().rows()))


@router.get("/sectors")
def sectors(include_stale: bool = False):
    jobs = get_screener_jobs()
    return jsonable({**sector_breadth(jobs.rows(), include_stale), **_meta(jobs.snapshot(rows=False))})


@router.get("/stocks")
def stocks(
    screen: str | None = Query(None, description="Preset id from /screens"),
    change_window: Window | None = Query(None, description="With min_change_pct: move over this window"),
    min_change_pct: float | None = Query(None, ge=-100, le=100, allow_inf_nan=False),
    rsi_min: float | None = Query(None, ge=0, le=100, allow_inf_nan=False),
    rsi_max: float | None = Query(None, ge=0, le=100, allow_inf_nan=False),
    macd: Literal["bullish", "bearish", "positive", "negative"] | None = Query(
        None, description="bullish/bearish: line vs signal; positive/negative: line vs zero (Groww's screen)"),
    near_52w: Literal["high", "low"] | None = None,
    breakout: Level | None = Query(None, description="Trading above this resistance"),
    near_level: Level | None = Query(None, description="Within 1% below this resistance"),
    index: str | None = Query(None, max_length=40, description="NSE index id, e.g. NIFTY100"),
    sector: list[str] | None = Query(None, description="NSE industry; repeat for several"),
    min_turnover_cr: float | None = Query(None, ge=0, allow_inf_nan=False),
    sort: str = "turnover",
    order: Literal["asc", "desc"] = "desc",
    limit: int = Query(100, ge=1, le=1000),
    include_stale: bool = False,
):
    if sort not in SORT_FIELDS:
        raise HTTPException(422, f"sort must be one of {sorted(SORT_FIELDS)}")
    filters: list[tuple] = []
    if screen is not None:
        if screen not in PRESETS:
            raise HTTPException(404, "Unknown screen")
        filters += PRESETS[screen]["filters"]
    if min_change_pct is not None:
        field = "change_pct" if change_window in (None, "1d") else f"change_{change_window}_pct"
        filters.append((field, "ge", min_change_pct))
    if rsi_min is not None:
        filters.append(("rsi", "ge", rsi_min))
    if rsi_max is not None:
        filters.append(("rsi", "le", rsi_max))
    if macd:
        field = "macd_hist" if macd in ("bullish", "bearish") else "macd"
        filters.append((field, "gt" if macd in ("bullish", "positive") else "le", 0))
    if near_52w:
        filters.append((f"year_{near_52w}_proximity_pct", "le", NEAR_52W_PCT))
    if breakout:
        filters.append((f"{breakout}_breakout", "eq", True))
    if near_level:
        filters.append((f"near_{near_level}", "eq", True))
    if index:
        filters.append(("indices", "has", index.strip().upper()))
    if sector:
        filters.append(("industry", "in", set(sector)))
    if min_turnover_cr is not None:
        filters.append(("turnover", "ge", min_turnover_cr * 1e7))
    jobs = get_screener_jobs()
    matched = filter_rows(jobs.rows(), filters, sort=sort, descending=order == "desc", include_stale=include_stale)
    return jsonable({"count": len(matched), "rows": matched[:limit], **_meta(jobs.snapshot(rows=False))})


@router.get("/symbols/{symbol}")
async def symbol_technicals(symbol: str):
    """Recompute one symbol now and compare with its saved screener row."""
    from trading_agent.analysis.screener import symbol_screen
    from trading_agent.ingest.sector_map import load_sector_map
    from trading_agent.storage.parquet_store import ParquetStore

    symbol = symbol.strip().upper()
    try:
        ParquetStore._validate_key(symbol)
    except ValueError as error:
        raise HTTPException(422, "Invalid symbol") from error
    def recompute():
        from trading_agent.ingest.live_quotes import fetch_quotes

        try:
            quote = fetch_quotes([symbol]).get(symbol)
        except Exception:
            quote = None  # no token or Kite down: stored candles only
        return symbol_screen(symbol, load_sector_map().get(symbol), quote=quote)

    fresh = await run_in_threadpool(recompute)
    if fresh is None:
        raise HTTPException(404, "Not enough stored minute history for screener technicals")
    saved = next((r for r in get_screener_jobs().rows() if r["symbol"] == symbol), None)
    changed = {} if saved is None else {
        key: {"saved": saved.get(key), "now": fresh[key]}
        for key in ("ltp", "change_pct", "rsi", "macd_hist", "r1", "s1", "r1_breakout", "s1_breakdown")
        if saved.get(key) != fresh[key]}
    return jsonable({"current": fresh, "saved_as_of": saved and saved["as_of"], "changed": changed})
