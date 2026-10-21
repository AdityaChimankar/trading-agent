"""FastAPI application for trading agent."""

from __future__ import annotations

from contextlib import asynccontextmanager
from secrets import compare_digest

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from trading_agent.api.routers import (
    digest,
    health,
    history,
    liveness,
    opportunities,
    pipeline,
    positions,
    research,
    strategy_lab,
    symbols,
    wallet,
    watchlist,
)
from trading_agent.core.config import get_settings
from trading_agent.core.logging_config import get_logger, setup_logging
from trading_agent.core.shutdown import get_shutdown_manager

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    # Startup
    setup_logging()
    logger.info("application_starting")

    shutdown_mgr = get_shutdown_manager()
    # Uvicorn owns process signals and invokes this lifespan for cleanup.
    # Replacing its handlers would leave SIGTERM waiting on an unused event.

    # Initialize database
    from trading_agent.storage.db import get_database

    db = get_database()
    worker = None
    try:
        await db.create_tables()
        from trading_agent.analysis.research_automation import get_research_automation
        worker = get_research_automation()
        # The worker starts disabled and performs no observations until enabled.
        # Its in-memory controller supports the documented single-worker deployment.
        worker.pause("API started. Enable automatic research explicitly for this session.")
        if get_settings().api.workers == 1:
            worker.start_worker()
        yield
    finally:
        logger.info("application_shutting_down")
        try:
            if worker is not None:
                await run_in_threadpool(worker.stop_worker)
        finally:
            try:
                await shutdown_mgr.run_cleanup()
            finally:
                await db.close()


def create_app() -> FastAPI:
    """Create and configure FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title="Trading Agent API",
        description="NSE Intraday Trading Signal Agent API",
        version="2.0.0",
        lifespan=lifespan,
    )

    # CORS
    @app.middleware("http")
    async def authenticate(request, call_next):
        token = settings.api.auth_token
        if token and request.method != "OPTIONS" and request.url.path not in {"/api/v1/health", "/api/v1/ready", "/api/v1/auth/status"}:
            supplied = request.headers.get("authorization", "")
            if not compare_digest(supplied.encode(), f"Bearer {token}".encode()):
                return JSONResponse(status_code=401, content={"detail": "Authentication required"})
        return await call_next(request)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.api.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/v1/auth/status", tags=["authentication"])
    async def auth_status():
        # Public capability only: never expose the token or broker credentials.
        return JSONResponse(
            content={"required": bool(settings.api.auth_token)},
            headers={"Cache-Control": "no-store"},
        )

    # Exception handlers
    @app.exception_handler(Exception)
    async def global_exception_handler(request, exc):
        logger.error("unhandled_exception", path=request.url.path, error=str(exc))
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )

    # Include routers
    app.include_router(health.router, prefix="/api/v1", tags=["health"])
    app.include_router(symbols.router, prefix="/api/v1/symbols", tags=["symbols"])
    app.include_router(history.router, prefix="/api/v1/history", tags=["history"])
    app.include_router(opportunities.router, prefix="/api/v1/opportunities", tags=["opportunities"])
    app.include_router(positions.router, prefix="/api/v1/positions", tags=["positions"])
    app.include_router(wallet.router, prefix="/api/v1/wallet", tags=["wallet"])
    app.include_router(pipeline.router, prefix="/api/v1/pipeline", tags=["pipeline"])
    app.include_router(research.router, prefix="/api/v1/research", tags=["research"])
    app.include_router(strategy_lab.router, prefix="/api/v1/strategy-lab", tags=["strategy-lab"])
    app.include_router(digest.router, prefix="/api/v1/digest", tags=["digest"])
    app.include_router(watchlist.router, prefix="/api/v1/watchlist", tags=["watchlist"])
    app.include_router(liveness.router, prefix="/api/v1/liveness", tags=["liveness"])

    @app.get("/")
    async def root():
        return {
            "name": "Trading Agent API",
            "version": "2.0.0",
            "status": "running",
        }

    return app


app = create_app()


def run():
    """Run the API server."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "trading_agent.api.main:app",
        host=settings.api.host,
        port=settings.api.port,
        reload=settings.api.reload,
        workers=settings.api.workers,
    )
