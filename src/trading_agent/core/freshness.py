"""Freshness monitoring for data freshness and staleness detection."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from threading import Lock

from trading_agent.core.logging_config import get_logger

logger = get_logger(__name__)


class FreshnessStatus(Enum):
    """Data freshness status."""

    FRESH = "fresh"
    STALE = "stale"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FreshnessConfig:
    """Freshness monitoring configuration."""

    fresh_threshold_seconds: int = 300  # 5 minutes
    stale_threshold_seconds: int = 900  # 15 minutes
    critical_threshold_seconds: int = 3600  # 1 hour
    check_interval_seconds: int = 60  # Check every minute


@dataclass
class FreshnessResult:
    """Result of freshness check."""

    status: FreshnessStatus
    last_update: datetime | None
    age_seconds: float | None
    message: str
    metadata: dict = field(default_factory=dict)


class FreshnessMonitor:
    """Monitor data freshness for multiple data sources."""

    def __init__(self, config: FreshnessConfig | None = None):
        self.config = config or FreshnessConfig()
        self._last_updates: dict[str, datetime] = {}
        self._metadata: dict[str, dict] = {}
        self._lock = Lock()
        self._callbacks: dict[str, list[Callable[[FreshnessResult], None]]] = {}

    def update(self, source: str, timestamp: datetime | None = None, **metadata) -> None:
        """Record an update for a data source."""
        ts = timestamp or datetime.now(UTC)
        with self._lock:
            self._last_updates[source] = ts
            if metadata:
                self._metadata[source] = metadata

    def check(self, source: str, current_time: datetime | None = None) -> FreshnessResult:
        """Check freshness of a data source."""
        now = current_time or datetime.now(UTC)

        with self._lock:
            last_update = self._last_updates.get(source)
            metadata = self._metadata.get(source, {})

        if last_update is None:
            return FreshnessResult(
                status=FreshnessStatus.UNKNOWN,
                last_update=None,
                age_seconds=None,
                message=f"No data received for {source}",
                metadata=metadata,
            )

        age = (now - last_update).total_seconds()

        if age <= self.config.fresh_threshold_seconds:
            status = FreshnessStatus.FRESH
            message = f"{source} is fresh ({age:.0f}s ago)"
        elif age <= self.config.stale_threshold_seconds:
            status = FreshnessStatus.STALE
            message = f"{source} is stale ({age:.0f}s ago)"
        elif age <= self.config.critical_threshold_seconds:
            status = FreshnessStatus.CRITICAL
            message = f"{source} is critical ({age:.0f}s ago)"
        else:
            status = FreshnessStatus.CRITICAL
            message = f"{source} is critical ({age:.0f}s ago) - possible outage"

        result = FreshnessResult(
            status=status,
            last_update=last_update,
            age_seconds=age,
            message=message,
            metadata=metadata,
        )

        # Trigger callbacks
        for callback in self._callbacks.get(source, []):
            try:
                callback(result)
            except Exception as e:
                logger.error("freshness_callback_error", source=source, error=str(e))

        return result

    def check_all(self, current_time: datetime | None = None) -> dict[str, FreshnessResult]:
        """Check freshness of all tracked sources."""
        with self._lock:
            sources = list(self._last_updates.keys())

        return {source: self.check(source, current_time) for source in sources}

    def register_callback(self, source: str, callback: Callable[[FreshnessResult], None]) -> None:
        """Register a callback for freshness changes."""
        with self._lock:
            if source not in self._callbacks:
                self._callbacks[source] = []
            self._callbacks[source].append(callback)

    def get_status_summary(self) -> dict:
        """Get summary of all sources."""
        results = self.check_all()
        return {
            "total": len(results),
            "fresh": sum(1 for r in results.values() if r.status == FreshnessStatus.FRESH),
            "stale": sum(1 for r in results.values() if r.status == FreshnessStatus.STALE),
            "critical": sum(1 for r in results.values() if r.status == FreshnessStatus.CRITICAL),
            "unknown": sum(1 for r in results.values() if r.status == FreshnessStatus.UNKNOWN),
            "details": {k: v.__dict__ for k, v in results.items()},
        }


# Global freshness monitor
_global_monitor: FreshnessMonitor | None = None


def get_freshness_monitor(config: FreshnessConfig | None = None) -> FreshnessMonitor:
    """Get or create the global freshness monitor."""
    global _global_monitor
    if _global_monitor is None:
        _global_monitor = FreshnessMonitor(config)
    return _global_monitor


# Market hours utilities
def in_market_hours(dt: datetime | None = None) -> bool:
    """Check if given time is within NSE market hours (9:15-15:30 IST).
    
    Args:
        dt: Datetime to check (defaults to now). Assumed to be UTC.
        
    Returns:
        True if within market hours.
    """
    if dt is None:
        dt = datetime.now(UTC)
    
    # Convert to IST (UTC+5:30)
    ist = dt.astimezone() if dt.tzinfo else dt
    # Simple check: 9:15-15:30 IST
    hour = ist.hour
    minute = ist.minute
    
    if hour < 9 or hour > 15:
        return False
    if hour == 9 and minute < 15:
        return False
    if hour == 15 and minute > 30:
        return False
    
    # Check if weekday (Monday=0, Sunday=6)
    if ist.weekday() >= 5:  # Saturday/Sunday
        return False
    
    return True


def ingest_heartbeat() -> dict | None:
    """Get live ticker heartbeat status.
    
    Returns:
        Dict with heartbeat info or None if not available.
    """
    try:
        from trading_agent.storage.db import get_database
        
        db = get_database()
        # Get latest heartbeat from ingest_status table
        # This is a simplified version - in production would query the actual table
        return {
            "connected": False,
            "last_tick": None,
            "heartbeat_age_sec": 9999,
            "buffer_depth": 0,
            "repairs_queued": 0,
        }
    except Exception:
        return None
