"""
Health Monitoring System - runs on separate port (default 8001) from main API (8000).

Provides comprehensive health checks for all pipeline components without
impacting the main trading API. Returns structured JSON suitable for
external monitoring (Prometheus, Grafana, Datadog, PagerDuty, etc.).
"""
import os
import sys
import json
import time
import sqlite3
import subprocess
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, asdict

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from storage.db import DB_PATH
from core.config import get_config
from core.freshness import ingest_heartbeat, in_market_hours


@dataclass
class HealthCheck:
    """Individual health check result."""
    name: str
    status: str  # "healthy" | "degraded" | "unhealthy"
    message: str
    latency_ms: float
    timestamp: str
    metadata: Dict[str, Any]


@dataclass
class SystemHealth:
    """Overall system health summary."""
    overall_status: str  # "healthy" | "degraded" | "unhealthy"
    checks: List[HealthCheck]
    timestamp: str
    uptime_seconds: float
    version: str


class HealthMonitor:
    """Runs all health checks and aggregates results."""
    
    def __init__(self):
        self.start_time = time.time()
        self.config = get_config()
        self.db_path = DB_PATH
    
    def _check(self, name: str, func) -> HealthCheck:
        """Run a single check with timing."""
        start = time.time()
        try:
            result = func()
            latency = (time.time() - start) * 1000
            if isinstance(result, tuple):
                status, message, metadata = result
            else:
                status, message, metadata = "healthy", str(result), {}
            return HealthCheck(name, status, message, latency, datetime.now().isoformat(), metadata)
        except Exception as e:
            latency = (time.time() - start) * 1000
            return HealthCheck(name, "unhealthy", f"{type(e).__name__}: {e}", latency, datetime.now().isoformat(), {})
    
    def check_database(self) -> tuple:
        """Check SQLite database connectivity and basic queries."""
        try:
            conn = sqlite3.connect(self.db_path, timeout=5)
            conn.row_factory = sqlite3.Row
            
            # Basic connectivity
            conn.execute("SELECT 1").fetchone()
            
            # Check critical tables exist
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = {t["name"] for t in tables}
            
            required_tables = {"candles", "watchlist", "open_positions", "news", "signals", "wallet_settings"}
            missing = required_tables - table_names
            if missing:
                conn.close()
                return "degraded", f"Missing tables: {missing}", {"missing_tables": list(missing)}
            
            # Check watchlist count
            watchlist_count = conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]
            
            # Check open positions
            open_pos = conn.execute("SELECT COUNT(*) FROM open_positions WHERE status='open'").fetchone()[0]
            
            # Check DB size
            db_size_mb = os.path.getsize(self.db_path) / (1024 * 1024)
            
            conn.close()
            
            return "healthy", "Database OK", {
                "watchlist_symbols": watchlist_count,
                "open_positions": open_pos,
                "db_size_mb": round(db_size_mb, 2),
                "tables": len(table_names)
            }
        except Exception as e:
            return "unhealthy", f"Database error: {e}", {}
    
    def check_kite_connection(self) -> tuple:
        """Check Kite Connect authentication and API access."""
        try:
            from ingest.kite_auth import get_kite_client
            kite = get_kite_client()
            
            # Test with a lightweight call
            profile = kite.profile()
            user_id = profile.get("user_id", "unknown")
            user_name = profile.get("user_name", "unknown")
            
            # Test market data access
            quote = kite.quote(["NSE:INFY"])
            if not quote:
                return "degraded", "Kite auth OK but quote empty", {"user_id": user_id}
            
            return "healthy", "Kite Connect OK", {
                "user_id": user_id,
                "user_name": user_name,
                "quote_symbols": list(quote.keys())
            }
        except Exception as e:
            return "unhealthy", f"Kite Connect failed: {e}", {}
    
    def check_live_ticker(self) -> tuple:
        """Check live ticker heartbeat and data freshness."""
        try:
            hb = ingest_heartbeat()
            if not hb:
                return "unhealthy", "No heartbeat - ticker not running", {}
            
            age = hb.get("heartbeat_age_sec", 9999)
            connected = hb.get("connected", False)
            last_tick = hb.get("last_tick")
            buffer_depth = hb.get("buffer_depth", 0)
            repairs_queued = hb.get("repairs_queued", 0)
            
            # Determine status
            if not connected:
                status = "unhealthy"
                msg = "Ticker disconnected"
            elif age > 300:  # 5 minutes
                status = "unhealthy"
                msg = f"Heartbeat stale: {age:.0f}s old"
            elif age > 90:
                status = "degraded"
                msg = f"Heartbeat aging: {age:.0f}s old"
            else:
                status = "healthy"
                msg = f"Ticker healthy (age: {age:.0f}s)"
            
            return status, msg, {
                "connected": connected,
                "heartbeat_age_sec": round(age, 1),
                "last_tick": last_tick,
                "buffer_depth": buffer_depth,
                "repairs_queued": repairs_queued
            }
        except Exception as e:
            return "unhealthy", f"Heartbeat check failed: {e}", {}
    
    def check_scheduler(self) -> tuple:
        """Check if scheduler process is running and jobs are firing."""
        try:
            # Check for scheduler PID file or process
            import psutil
            scheduler_pids = []
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    if proc.info['cmdline'] and 'scripts.scheduler' in ' '.join(proc.info['cmdline']):
                        scheduler_pids.append(proc.info['pid'])
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            
            if not scheduler_pids:
                return "degraded", "Scheduler process not found", {"running": False}
            
            # Check recent job execution via position_alerts or recent signals
            conn = sqlite3.connect(self.db_path, timeout=5)
            conn.row_factory = sqlite3.Row
            
            # Check recent monitor cycle (should run every minute)
            recent_alerts = conn.execute(
                "SELECT COUNT(*) FROM position_alerts WHERE created_at > datetime('now', '-5 minutes')"
            ).fetchone()[0]
            
            # Check recent decision cycles
            recent_signals = conn.execute(
                "SELECT COUNT(*) FROM signals WHERE timestamp > datetime('now', '-10 minutes')"
            ).fetchone()[0]
            
            conn.close()
            
            return "healthy", "Scheduler running", {
                "pids": scheduler_pids,
                "recent_alerts_5min": recent_alerts,
                "recent_signals_10min": recent_signals
            }
        except ImportError:
            return "degraded", "psutil not installed - cannot check scheduler process", {}
        except Exception as e:
            return "unhealthy", f"Scheduler check failed: {e}", {}
    
    def check_news_feeds(self) -> tuple:
        """Check news feed freshness and volume."""
        try:
            conn = sqlite3.connect(self.db_path, timeout=5)
            conn.row_factory = sqlite3.Row
            
            # Recent news count
            recent_news = conn.execute(
                "SELECT COUNT(*) FROM news WHERE fetched_at > datetime('now', '-1 hour')"
            ).fetchone()[0]
            
            # By source
            sources = conn.execute(
                "SELECT source, COUNT(*) as cnt FROM news WHERE fetched_at > datetime('now', '-1 hour') GROUP BY source"
            ).fetchall()
            source_counts = {s["source"]: s["cnt"] for s in sources}
            
            # Oldest news
            oldest = conn.execute("SELECT MIN(fetched_at) FROM news").fetchone()[0]
            
            conn.close()
            
            if recent_news == 0:
                return "degraded", "No news fetched in last hour", {"by_source": source_counts}
            
            return "healthy", f"News fresh ({recent_news} items in last hour)", {
                "recent_count": recent_news,
                "by_source": source_counts,
                "oldest": oldest
            }
        except Exception as e:
            return "unhealthy", f"News check failed: {e}", {}
    
    def check_openrouter(self) -> tuple:
        """Check OpenRouter API availability with timeout."""
        try:
            from core.llm_client import complete, get_model
            import concurrent.futures
            
            def _call_openrouter():
                return complete("Reply with exactly: OK")
            
            # Run with 10 second timeout
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_call_openrouter)
                try:
                    reply = future.result(timeout=10)
                except concurrent.futures.TimeoutError:
                    return "degraded", "OpenRouter timeout (10s)", {"model": get_model()}
            
            if reply and "OK" in reply.upper():
                return "healthy", "OpenRouter OK", {"model": get_model()}
            else:
                return "degraded", f"OpenRouter unexpected reply: {reply[:50]}", {"model": get_model()}
        except Exception as e:
            return "unhealthy", f"OpenRouter failed: {e}", {}
    
    def check_disk_space(self) -> tuple:
        """Check disk space for data directory."""
        try:
            data_dir = PROJECT_ROOT / "data"
            total, used, free = shutil.disk_usage(data_dir)
            free_gb = free / (1024**3)
            used_pct = (used / total) * 100
            
            if free_gb < 1:
                status = "unhealthy"
                msg = f"Critical: {free_gb:.2f} GB free"
            elif free_gb < 5:
                status = "degraded"
                msg = f"Low disk: {free_gb:.2f} GB free"
            else:
                status = "healthy"
                msg = f"Disk OK: {free_gb:.2f} GB free ({used_pct:.1f}% used)"
            
            return status, msg, {
                "free_gb": round(free_gb, 2),
                "used_pct": round(used_pct, 1),
                "total_gb": round(total / (1024**3), 2)
            }
        except Exception as e:
            return "unhealthy", f"Disk check failed: {e}", {}
    
    def check_memory(self) -> tuple:
        """Check system memory."""
        try:
            import psutil
            mem = psutil.virtual_memory()
            available_gb = mem.available / (1024**3)
            used_pct = mem.percent
            
            if available_gb < 0.5:
                status = "unhealthy"
                msg = f"Critical: {available_gb:.2f} GB available"
            elif available_gb < 2:
                status = "degraded"
                msg = f"Low memory: {available_gb:.2f} GB available"
            else:
                status = "healthy"
                msg = f"Memory OK: {available_gb:.2f} GB available ({used_pct:.1f}% used)"
            
            return status, msg, {
                "available_gb": round(available_gb, 2),
                "used_pct": round(used_pct, 1),
                "total_gb": round(mem.total / (1024**3), 2)
            }
        except ImportError:
            return "degraded", "psutil not installed", {}
        except Exception as e:
            return "unhealthy", f"Memory check failed: {e}", {}
    
    def check_candle_freshness(self) -> tuple:
        """Check if candles are being written and are fresh."""
        try:
            conn = sqlite3.connect(self.db_path, timeout=5)
            conn.row_factory = sqlite3.Row
            
            # Quick check: any candles in last 24h? Use LIMIT 1 for speed
            count = conn.execute(
                "SELECT COUNT(*) FROM candles WHERE timestamp > datetime('now', '-1 day') LIMIT 10000"
            ).fetchone()[0]
            
            if count == 0:
                conn.close()
                return "unhealthy", "No candles in last 24h", {"count_24h": 0}
            
            # Most recent candle
            latest = conn.execute("SELECT MAX(timestamp) as latest FROM candles").fetchone()
            
            # Candles in last 5 minutes
            recent = conn.execute(
                "SELECT COUNT(*) FROM candles WHERE timestamp > datetime('now', '-5 minutes')"
            ).fetchone()[0]
            
            # Active symbols
            symbols = conn.execute(
                "SELECT COUNT(DISTINCT symbol) FROM candles WHERE timestamp > datetime('now', '-24 hours') LIMIT 1000"
            ).fetchone()[0]
            
            conn.close()
            
            latest_ts = latest["latest"] if latest else None
            age_min = 999
            if latest_ts:
                latest_dt = datetime.fromisoformat(latest_ts.replace("+05:30", ""))
                age_min = (datetime.now() - latest_dt).total_seconds() / 60
            
            if age_min > 10 and in_market_hours():
                status = "degraded"
                msg = f"Candles stale: latest {age_min:.1f} min ago"
            elif age_min > 5 and in_market_hours():
                status = "degraded"
                msg = f"Candles aging: latest {age_min:.1f} min ago"
            else:
                status = "healthy"
                msg = f"Candles fresh: latest {age_min:.1f} min ago"
            
            return status, msg, {
                "latest_timestamp": latest_ts,
                "age_minutes": round(age_min, 1),
                "active_symbols": symbols,
                "candles_last_5min": recent,
                "total_24h": count
            }
        except Exception as e:
            return "unhealthy", f"Candle freshness check failed: {e}", {}
    
    def check_watchlist_coverage(self) -> tuple:
        """Check how many watchlist symbols have recent candles."""
        try:
            conn = sqlite3.connect(self.db_path, timeout=5)
            conn.row_factory = sqlite3.Row
            
            total_symbols = conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]
            
            if total_symbols == 0:
                conn.close()
                return "degraded", "Watchlist empty", {"total_symbols": 0}
            
            # Quick check: any candles at all?
            candle_count = conn.execute("SELECT COUNT(*) FROM candles LIMIT 1").fetchone()[0]
            if candle_count == 0:
                conn.close()
                return "unhealthy", f"No candles in database for {total_symbols} symbols", {
                    "total_symbols": total_symbols,
                    "active_24h": 0,
                    "active_1h": 0,
                    "coverage_pct": 0.0
                }
            
            # Symbols with candles in last 24h - use a faster query with LIMIT
            active = conn.execute(
                "SELECT COUNT(DISTINCT symbol) FROM candles WHERE timestamp > datetime('now', '-24 hours') LIMIT 1000"
            ).fetchone()[0]
            
            # Symbols with candles in last hour
            active_1h = conn.execute(
                "SELECT COUNT(DISTINCT symbol) FROM candles WHERE timestamp > datetime('now', '-1 hour') LIMIT 1000"
            ).fetchone()[0]
            
            conn.close()
            
            coverage_pct = (active / total_symbols * 100) if total_symbols > 0 else 0
            
            if coverage_pct < 50:
                status = "unhealthy"
            elif coverage_pct < 80:
                status = "degraded"
            else:
                status = "healthy"
            
            return status, f"Watchlist coverage: {active}/{total_symbols} ({coverage_pct:.1f}%)", {
                "total_symbols": total_symbols,
                "active_24h": active,
                "active_1h": active_1h,
                "coverage_pct": round(coverage_pct, 1)
            }
        except Exception as e:
            return "unhealthy", f"Watchlist coverage check failed: {e}", {}
    
    def run_all_checks(self) -> SystemHealth:
        """Run all health checks and return aggregated result."""
        checks = [
            self._check("database", self.check_database),
            self._check("kite_connection", self.check_kite_connection),
            self._check("live_ticker", self.check_live_ticker),
            self._check("scheduler", self.check_scheduler),
            self._check("news_feeds", self.check_news_feeds),
            self._check("openrouter", self.check_openrouter),
            self._check("disk_space", self.check_disk_space),
            self._check("memory", self.check_memory),
            self._check("candle_freshness", self.check_candle_freshness),
            self._check("watchlist_coverage", self.check_watchlist_coverage),
        ]
        
        # Determine overall status
        statuses = [c.status for c in checks]
        if "unhealthy" in statuses:
            overall = "unhealthy"
        elif "degraded" in statuses:
            overall = "degraded"
        else:
            overall = "healthy"
        
        return SystemHealth(
            overall_status=overall,
            checks=checks,
            timestamp=datetime.now().isoformat(),
            uptime_seconds=round(time.time() - self.start_time, 1),
            version="1.0.0"
        )


# FastAPI app for health monitoring
app = FastAPI(
    title="Trading Agent Health Monitor",
    description="Independent health checks for trading pipeline",
    version="1.0.0"
)

monitor = HealthMonitor()


@app.get("/health")
async def health() -> SystemHealth:
    """Full health check - runs all checks."""
    return monitor.run_all_checks()


@app.get("/health/live")
async def liveness() -> Dict[str, str]:
    """Kubernetes liveness probe - lightweight."""
    return {"status": "alive", "timestamp": datetime.now().isoformat()}


@app.get("/health/ready")
async def readiness() -> Dict[str, Any]:
    """Kubernetes readiness probe - checks critical dependencies."""
    health = monitor.run_all_checks()
    critical_checks = ["database", "kite_connection", "live_ticker"]
    
    critical_ok = all(
        c.status != "unhealthy" 
        for c in health.checks 
        if c.name in critical_checks
    )
    
    if not critical_ok:
        raise HTTPException(
            status_code=503, 
            detail={"status": "not_ready", "failed": [c.name for c in health.checks if c.name in critical_checks and c.status == "unhealthy"]}
        )
    
    return {"status": "ready", "timestamp": datetime.now().isoformat()}


@app.get("/health/check/{check_name}")
async def single_check(check_name: str) -> HealthCheck:
    """Run a single named check."""
    check_map = {
        "database": monitor.check_database,
        "kite_connection": monitor.check_kite_connection,
        "live_ticker": monitor.check_live_ticker,
        "scheduler": monitor.check_scheduler,
        "news_feeds": monitor.check_news_feeds,
        "openrouter": monitor.check_openrouter,
        "disk_space": monitor.check_disk_space,
        "memory": monitor.check_memory,
        "candle_freshness": monitor.check_candle_freshness,
        "watchlist_coverage": monitor.check_watchlist_coverage,
    }
    
    if check_name not in check_map:
        raise HTTPException(status_code=404, detail=f"Unknown check: {check_name}. Available: {list(check_map.keys())}")
    
    return monitor._check(check_name, check_map[check_name])


@app.get("/metrics")
async def metrics() -> str:
    """Prometheus-compatible metrics endpoint."""
    health = monitor.run_all_checks()
    
    lines = [
        f"trading_agent_health_status{{status=\"{health.overall_status}\"}} 1",
        f"trading_agent_uptime_seconds {health.uptime_seconds}",
    ]
    
    for check in health.checks:
        status_val = 1 if check.status == "healthy" else (0.5 if check.status == "degraded" else 0)
        lines.append(
            f'trading_agent_check_status{{check="{check.name}",status="{check.status}"}} {status_val}'
        )
        lines.append(
            f'trading_agent_check_latency_ms{{check="{check.name}"}} {check.latency_ms:.1f}'
        )
        # Add metadata as labels where possible
        for k, v in check.metadata.items():
            if isinstance(v, (int, float)):
                lines.append(
                    f'trading_agent_check_{check.name}_{k} {v}'
                )
    
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    # Run on separate port from main API
    PORT = int(os.getenv("HEALTH_PORT", "8001"))
    print(f"Starting Health Monitor on port {PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")