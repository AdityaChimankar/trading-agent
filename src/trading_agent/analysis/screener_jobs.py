"""Background screener scan: one row per stored symbol, saved for the dashboard.

Reading ~2,300 symbols' minute candles takes minutes, so the dashboard never
computes the screener on a page load. A scan is started explicitly ("Update
technicals", ``POST /api/v1/screener/run`` or ``trading-agent screener-scan``)
and its rows are saved to ``data/research/screener.json``; every GET reads that
snapshot. ``--watch`` repeats the scan during market hours.
"""

from __future__ import annotations

import copy
import json
import multiprocessing
import os
import threading
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

from filelock import FileLock, Timeout

from trading_agent.analysis.screener import (
    MIN_MACD_SESSIONS,
    MIN_RSI_SESSIONS,
    STALE_LAG_MINUTES,
    mark_stale,
    symbol_screen,
)
from trading_agent.core.config import get_settings
from trading_agent.ingest.sector_map import load_sector_map

UNIVERSES = ("all", "indexed")
# Processes, not threads: the per-symbol pandas work holds the GIL, so 4 threads
# took 9 minutes for 2,312 symbols on 2026-10-26. "spawn" is safe from the API's threads.
WORKERS = max(1, min(4, (os.cpu_count() or 2) - 1))
CHUNK = 16


def _worker_init() -> None:
    # Each symbol is read once per scan, so the store's read cache only costs memory:
    # one process grew to 1.3 GB over 2,312 symbols, and four of them broke the pool.
    from trading_agent.storage.parquet_store import get_parquet_store

    get_parquet_store().cache_rows = 0


def _screen_one(task: tuple) -> tuple[str, dict | None, str | None]:
    symbol, info, now, quote = task
    try:
        return symbol, symbol_screen(symbol, info, now, quote), None
    except Exception as error:
        return symbol, None, type(error).__name__


def technicals_check(rows: list[dict], now: datetime | None = None) -> dict:
    """How far the snapshot's technicals can be trusted: history depth, staleness, bar age."""
    now = now or datetime.now(UTC)
    current = [r for r in rows if not r.get("stale")]
    ages = sorted((now - datetime.fromisoformat(r["as_of"])).total_seconds() / 60 for r in current)
    latest = max((r["session"] for r in rows), default=None)
    return {
        "rows": len(rows), "current": len(current), "stale": len(rows) - len(current),
        "lagging": sum(r.get("stale", False) and r["session"] == latest for r in rows),
        "no_rsi": sum(r["rsi"] is None for r in current),
        "no_macd": sum(r["macd_hist"] is None for r in current),
        "partial_52w": sum(not r["full_year"] for r in current),
        "quoted": sum(r.get("price_source") == "quote" for r in current),
        "unclassified": sum(not r.get("industry") for r in current),
        "median_bar_age_minutes": round(ages[len(ages) // 2], 1) if ages else None,
        "notes": [f"Stale rows (older session, or over {STALE_LAG_MINUTES} min behind the newest bar: "
                  "'lagging', usually a ticker gap) are left out of screens and sector breadth.",
                  f"RSI(14) needs {MIN_RSI_SESSIONS} completed sessions and MACD(12,26,9) {MIN_MACD_SESSIONS}; "
                  "fewer leaves the field empty and the row out of that screen.",
                  "52-week range uses the stored sessions when under a year (partial_52w)."],
    }


class ScreenerJobs:
    def __init__(self):
        self.lock = threading.RLock()
        self.cancelled = threading.Event()
        self.state = {"status": "idle", "processed": 0, "total": 0, "rows": [], "errors": [], "skipped": 0}
        self.path = get_settings().data_dir / "research" / "screener.json"
        self.run_lock = FileLock(str(self.path) + ".run.lock", timeout=0, thread_local=False)
        if self.path.exists():
            try:
                self.state = json.loads(self.path.read_text(encoding="utf-8"))
                if self.state["status"] == "running":
                    self.state["status"] = "interrupted"
            except (ValueError, KeyError):
                pass
        # The last completed rows stay readable while a new scan runs.
        self.previous_rows = self.state.get("rows", []) if self.state.get("status") == "completed" else []

    def snapshot(self, rows: bool = True) -> dict:
        with self.lock:
            state = copy.deepcopy(self.state if rows else {k: v for k, v in self.state.items() if k != "rows"})
            if rows and state["status"] == "running":
                state["rows"] = copy.deepcopy(self.previous_rows)
        return state

    def rows(self) -> list[dict]:
        with self.lock:
            return copy.deepcopy(self.previous_rows if self.state["status"] == "running" else self.state["rows"])

    def save(self):
        from trading_agent.api.serializers import jsonable

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".screener-{uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(jsonable(self.state), allow_nan=False), encoding="utf-8")
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def start(self, universe: str = "all", symbols: list[str] | None = None, quotes: bool = True) -> dict:
        if universe not in UNIVERSES:
            raise ValueError(f"Universe must be one of {UNIVERSES}")
        from trading_agent.storage.parquet_store import get_parquet_store

        sector_map = load_sector_map()
        with self.lock:
            if self.state["status"] == "running":
                raise RuntimeError("A screener scan is already running")
            if symbols is None:
                store = get_parquet_store()
                symbols = [s for s in store.get_symbols() if "minute" in store.get_intervals(s)]
                if universe == "indexed":
                    symbols = [s for s in symbols if s in sector_map]
            symbols = sorted(set(symbols))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.run_lock.acquire()
            except Timeout as error:
                raise RuntimeError("Another process is running a screener scan") from error
            self.cancelled.clear()
            if self.state.get("status") == "completed":
                self.previous_rows = self.state["rows"]
            self.state = {"status": "running", "processed": 0, "total": len(symbols), "rows": [],
                          "errors": [], "skipped": 0, "universe": universe,
                          "started_at": datetime.now(UTC).isoformat(),
                          "sector_map_loaded": bool(sector_map)}
            try:
                self.save()
                threading.Thread(target=self.run, args=(symbols, sector_map, quotes), daemon=True,
                                 name="screener-scan").start()
            except Exception:
                self.state["status"] = "error"
                self.state["errors"].append({"symbol": None, "reason": "Scan could not start"})
                self.run_lock.release()
                raise
        return self.snapshot(rows=False)

    def _quotes(self, symbols: list[str], wanted: bool) -> dict:
        """Live quotes for the scan, or {} with the reason recorded (no token, Kite error)."""
        if not wanted:
            status = {"status": "disabled"}
            quotes = {}
        else:
            from trading_agent.ingest.live_quotes import fetch_quotes

            try:
                quotes = fetch_quotes(symbols)
                status = {"status": "ok", "returned": len(quotes), "requested": len(symbols)}
            except Exception as error:
                quotes = {}
                status = {"status": "unavailable", "reason": type(error).__name__}
        with self.lock:
            self.state["quotes"] = status
        return quotes

    def run(self, symbols: list[str], sector_map: dict, quotes_wanted: bool = True):
        try:
            quotes = self._quotes(symbols, quotes_wanted)
            now = datetime.now(UTC)
            rows = []
            tasks = [(s, sector_map.get(s), now, quotes.get(s)) for s in symbols]
            pool = ProcessPoolExecutor(max_workers=WORKERS, mp_context=multiprocessing.get_context("spawn"),
                                       initializer=_worker_init)
            try:
                for symbol, row, error in pool.map(_screen_one, tasks, chunksize=CHUNK):
                    if self.cancelled.is_set():
                        break
                    with self.lock:
                        if row is not None:
                            rows.append(row)
                        elif error:
                            self.state["errors"].append({"symbol": symbol, "reason": error})
                        else:
                            self.state["skipped"] += 1  # under two stored sessions
                        self.state["processed"] += 1
                        self.state["current_symbol"] = symbol
            finally:
                pool.shutdown(wait=True, cancel_futures=True)
            latest = mark_stale(rows)
            with self.lock:
                cancelled = self.cancelled.is_set()
                self.state.update(rows=rows, status="cancelled" if cancelled else "completed",
                                  completed_at=datetime.now(UTC).isoformat(), latest_session=latest,
                                  check=technicals_check(rows))
                if not cancelled:
                    self.previous_rows = rows
                self.save()
        except Exception as error:
            with self.lock:
                self.state["status"] = "error"
                self.state["errors"].append({"symbol": None, "reason": type(error).__name__})
                self.save()
        finally:
            self.run_lock.release()


_jobs = None
_init_lock = threading.Lock()


def get_screener_jobs() -> ScreenerJobs:
    global _jobs
    with _init_lock:
        if _jobs is None:
            _jobs = ScreenerJobs()
    return _jobs
