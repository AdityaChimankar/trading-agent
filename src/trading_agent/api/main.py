"""FastAPI application for trading agent."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from trading_agent.api.routers import (
    digest,
    health,
    history,
    liveness,
    opportunities,
    pipeline,
    positions,
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
    shutdown_mgr.register_signals()

    # Initialize database
    from trading_agent.storage.db import get_database

    db = get_database()
    await db.create_tables()

    yield

    # Shutdown
    logger.info("application_shutting_down")
    await shutdown_mgr.run_cleanup()
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
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.api.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
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
