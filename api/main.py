"""
FastAPI backend for the React dashboard.

Run with:
    uvicorn api.main:app --reload --port 8000

Serves JSON over the existing Python modules (rankings, position_monitor,
opportunity_finder, portfolio_risk, quant_indicators, ...) - no trading
logic lives here, it only reads from the same modules the Streamlit
dashboard used.

init_db() is called on startup for the same reason dashboard.py called
it: CREATE TABLE IF NOT EXISTS won't add a column to a table that
already exists, so a schema change only takes effect once something
actually runs the migration.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routers import digest, liveness, opportunities, positions, symbols, watchlist, wallet
from storage.db import init_db


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Intraday Trading Agent API",
    version="1.0.0",
    description="Read-only views over the local trading-agent SQLite DB, plus "
                "paper-position open/close. No orders are ever placed.",
    lifespan=lifespan,
)

# The Vite dev server runs on a different origin (5173), so the browser
# needs CORS to call this API directly. In production the built front end
# is served from the same origin and this is a no-op.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health", tags=["meta"])
def health():
    return {"status": "ok"}


@app.get("/api/health/ready", tags=["meta"])
def health_ready():
    """Deeper readiness probe than /api/health: does the database exist, is
    the watchlist populated, and is the live feed actually delivering?

    Kept separate from /api/health so a process/supervisor check ("is the API
    up?") never fails just because the market feed is down - those are two
    different failures and they need different fixes.
    """
    from core.freshness import feed_summary
    from storage.db import get_watchlist_symbols

    symbols = get_watchlist_symbols()
    summary = feed_summary(symbols)
    return {
        "database": "ok",
        "watchlist_symbols": len(symbols),
        "feed_status": summary["status"],
        "market_open": summary["market_open"],
        "stale_symbols": summary["stale_symbols"],
    }


app.include_router(liveness.router)
app.include_router(watchlist.router)
app.include_router(positions.router)
app.include_router(opportunities.router)
app.include_router(digest.router)
app.include_router(symbols.router)
app.include_router(wallet.router)
