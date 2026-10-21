"""
Health check endpoints for all system components.

Provides comprehensive health information for monitoring and debugging.
"""
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.circuit_breaker import get_circuit_status
from core.freshness import feed_summary
from core.shutdown import is_shutting_down
from storage.db import get_connection, get_watchlist_symbols

router = APIRouter(prefix="/api/health", tags=["health"])


class ComponentHealth(BaseModel):
    """Health status for a single component."""
    name: str
    status: str  # "healthy", "degraded", "unhealthy", "unknown"
    details: dict[str, Any] = {}
    checked_at: str


class HealthResponse(BaseModel):
    """Overall health response."""
    status: str  # "healthy", "degraded", "unhealthy"
    timestamp: str
    components: list[ComponentHealth]
    shutdown_initiated: bool


def check_database() -> ComponentHealth:
    """Check database connectivity and basic health."""
    try:
        conn = get_connection()
        # Test basic query
        result = conn.execute("SELECT 1").fetchone()
        conn.close()

        # Check WAL mode
        conn = get_connection()
        wal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()

        return ComponentHealth(
            name="database",
            status="healthy" if wal_mode == "wal" else "degraded",
            details={"wal_mode": wal_mode},
            checked_at=datetime.now().isoformat(),
        )
    except Exception as e:
        return ComponentHealth(
            name="database",
            status="unhealthy",
            details={"error": str(e)},
            checked_at=datetime.now().isoformat(),
        )


def check_watchlist() -> ComponentHealth:
    """Check watchlist is populated."""
    try:
        symbols = get_watchlist_symbols()
        return ComponentHealth(
            name="watchlist",
            status="healthy" if symbols else "degraded",
            details={"symbol_count": len(symbols)},
            checked_at=datetime.now().isoformat(),
        )
    except Exception as e:
        return ComponentHealth(
            name="watchlist",
            status="unhealthy",
            details={"error": str(e)},
            checked_at=datetime.now().isoformat(),
        )


def check_feed() -> ComponentHealth:
    """Check live feed status."""
    try:
        symbols = get_watchlist_symbols()
        summary = feed_summary(symbols)

        # Map feed status to health status
        status_map = {
            "live": "healthy",
            "degraded": "degraded",
            "down": "unhealthy",
            "closed": "healthy",  # Market closed is expected
        }

        return ComponentHealth(
            name="live_feed",
            status=status_map.get(summary.get("status", "unknown"), "unknown"),
            details=summary,
            checked_at=datetime.now().isoformat(),
        )
    except Exception as e:
        return ComponentHealth(
            name="live_feed",
            status="unhealthy",
            details={"error": str(e)},
            checked_at=datetime.now().isoformat(),
        )


def check_circuit_breakers() -> ComponentHealth:
    """Check circuit breaker status."""
    try:
        status = get_circuit_status()
        unhealthy = any(s["state"] == "open" for s in status.values())

        return ComponentHealth(
            name="circuit_breakers",
            status="unhealthy" if unhealthy else "healthy",
            details=status,
            checked_at=datetime.now().isoformat(),
        )
    except Exception as e:
        return ComponentHealth(
            name="circuit_breakers",
            status="unknown",
            details={"error": str(e)},
            checked_at=datetime.now().isoformat(),
        )


def check_ml_model() -> ComponentHealth:
    """Check ML model availability."""
    try:
        from paths import ML_MODEL_PATH
        if ML_MODEL_PATH.exists():
            # Try to load model to verify it's valid
            import joblib
            bundle = joblib.load(ML_MODEL_PATH)
            model_type = type(bundle.get("model", "")).__name__ if bundle.get("model") else "unknown"
            feature_count = len(bundle.get("feature_columns", []))

            return ComponentHealth(
                name="ml_model",
                status="healthy",
                details={
                    "model_type": model_type,
                    "feature_count": feature_count,
                    "path": str(ML_MODEL_PATH),
                },
                checked_at=datetime.now().isoformat(),
            )
        else:
            return ComponentHealth(
                name="ml_model",
                status="degraded",
                details={"error": "Model file not found", "path": str(ML_MODEL_PATH)},
                checked_at=datetime.now().isoformat(),
            )
    except Exception as e:
        return ComponentHealth(
            name="ml_model",
            status="unhealthy",
            details={"error": str(e)},
            checked_at=datetime.now().isoformat(),
        )


def check_recent_signals() -> ComponentHealth:
    """Check if signals are being generated recently."""
    try:
        conn = get_connection()
        # Check signals from last hour
        result = conn.execute("""
            SELECT COUNT(*) as cnt
            FROM signals
            WHERE timestamp >= datetime('now', '-1 hour')
        """).fetchone()
        conn.close()

        count = result["cnt"] if result else 0
        return ComponentHealth(
            name="signal_generation",
            status="healthy" if count > 0 else "degraded",
            details={"signals_last_hour": count},
            checked_at=datetime.now().isoformat(),
        )
    except Exception as e:
        return ComponentHealth(
            name="signal_generation",
            status="unknown",
            details={"error": str(e)},
            checked_at=datetime.now().isoformat(),
        )


@router.get("", response_model=HealthResponse)
def health_check():
    """Comprehensive health check endpoint."""
    components = [
        check_database(),
        check_watchlist(),
        check_feed(),
        check_circuit_breakers(),
        check_ml_model(),
        check_recent_signals(),
    ]

    # Determine overall status
    statuses = [c.status for c in components]
    if "unhealthy" in statuses:
        overall = "unhealthy"
    elif "degraded" in statuses:
        overall = "degraded"
    else:
        overall = "healthy"

    return HealthResponse(
        status=overall,
        timestamp=datetime.now().isoformat(),
        components=components,
        shutdown_initiated=is_shutting_down(),
    )


@router.get("/live")
def liveness_probe():
    """Kubernetes-style liveness probe - just checks if process is alive."""
    return {"status": "alive" if not is_shutting_down() else "shutting_down"}


@router.get("/ready")
def readiness_probe():
    """Kubernetes-style readiness probe - checks if service can handle requests."""
    # Check critical components
    db_check = check_database()
    feed_check = check_feed()

    ready = db_check.status != "unhealthy"

    if not ready:
        raise HTTPException(
            status_code=503,
            detail={
                "ready": False,
                "database": db_check.status,
                "feed": feed_check.status,
            }
        )

    return {
        "ready": True,
        "database": db_check.status,
        "feed": feed_check.status,
    }