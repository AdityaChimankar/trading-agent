"""Opt-in, single-process research automation. Never executes broker actions.

Only scans stored data. No training, promotion, validation runs, news refresh,
login, backfill or paper/live orders belong in this worker. Arming is deliberately
in memory: restarting the API always requires the operator to enable it again.
"""
from __future__ import annotations

import copy
import threading
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from trading_agent.analysis.candle_changes import CandleObservationPending
from trading_agent.analysis.research_jobs import get_research_jobs
from trading_agent.risk.ledger_guard import read_ledger_state


def research_session_open(now: datetime) -> bool:
    """Configured weekday entry window, not an exchange holiday calendar."""
    local = now.astimezone(ZoneInfo("Asia/Kolkata"))
    return local.weekday() < 5 and time(9, 15) <= local.time() < time(15, 20)


class ResearchAutomation:
    CHECK_SECONDS = 30
    # The live set's 1-minute plans expire ~75 s after their bar closes, so its
    # new bars are looked for more often.
    LIVE_CHECK_SECONDS = 10
    MAX_FAILURES = 3

    def __init__(self, jobs=None, observe=None, ledger=None, clock=None, symbols=None):
        self._jobs = jobs or get_research_jobs
        self._observe = observe
        self._ledger = ledger or read_ledger_state
        self._clock = clock or (lambda: datetime.now(UTC))
        self._symbols = symbols
        self._lock = threading.RLock()
        self._tick_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._generation = 0
        self._consumed = None
        self._pending = None
        self._owned_scan = None
        self._last_finish = None
        self._retry_at = None
        self._due = None
        self._warmup_started = None
        self.state = {
            "enabled": False, "status": "disabled",
            "reason": "Research automation is off. Enable explicitly for this API session.",
            "options": None, "min_scan_seconds": 120,
            "last_check_at": None, "next_check_at": None,
            "last_trigger_at": None, "scan_started_at": None,
            "consecutive_failures": 0, "data": None,
            "capabilities": {"orders": False, "training": False,
                             "promotion": False, "external_requests": False},
        }

    def snapshot(self):
        with self._lock:
            result = copy.deepcopy(self.state)
            result["running"] = bool(self._thread and self._thread.is_alive() and not self._stop.is_set())
            return result

    def enable(self, options, min_scan_seconds=120):
        # Defense in depth for non-HTTP callers. The API additionally validates
        # numeric settings through ScanRequest before reaching this controller.
        if options.get("require_validated") is not True or options.get("symbols") is not None:
            raise ValueError("Automation requires qualified evidence and the full configured universe")
        if options.get("scope", "universe") not in {"universe", "live"}:
            raise ValueError("Automation scope must be the universe or the live set")
        if options.get("scope") == "live":
            from trading_agent.analysis.research_engine import live_symbols
            if not live_symbols():
                raise ValueError("The live scope needs live_symbols.txt")
        if options.get("interval") not in {"minute", "5minute", "15minute"}:
            raise ValueError("Automation supports intraday candles only")
        if not 60 <= min_scan_seconds <= 3600:
            raise ValueError("Minimum scan delay must be between 60 and 3600 seconds")
        with self._lock:
            if self.state["enabled"]:
                raise RuntimeError("Pause automation before changing its settings")
            self._generation += 1
            self._consumed = self._pending = self._owned_scan = None
            self._last_finish = self._retry_at = self._due = None
            self._warmup_started = None
            self.state.update(enabled=True, status="waiting_changes",
                              reason="Checking stored candles before starting a qualified research scan.",
                              options=copy.deepcopy(options), min_scan_seconds=min_scan_seconds,
                              consecutive_failures=0, data=None, next_check_at=None,
                              last_trigger_at=None, scan_started_at=None)
            self._wake.set()
            return self.snapshot()

    def pause(self, reason="Paused by operator. No new automatic scans will start."):
        with self._lock:
            self._generation += 1
            # Do not cancel an unrelated manual scan.
            if self._owned_scan is not None:
                jobs = self._jobs()
                with jobs.lock:
                    if jobs.state.get("started_at") == self._owned_scan and jobs.state.get("source") == "automation":
                        jobs.cancelled.set()
            self.state.update(enabled=False, status="paused", reason=reason, next_check_at=None)
            self._due = None
            return self.snapshot()

    def start_worker(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                if self._stop.is_set():
                    raise RuntimeError("Previous research worker is still stopping; retry after it exits")
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="research-automation", daemon=True)
            self._thread.start()

    def stop_worker(self):
        self.pause("API stopped. Enable research automation again after restarting.")
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def _run(self):
        while not self._stop.is_set():
            self._wake.clear()
            self.tick()
            self._wake.wait(timeout=5)

    def _set(self, status, reason):
        self.state.update(status=status, reason=reason)

    def _failed(self, now, reason):
        self.state["consecutive_failures"] += 1
        failures = self.state["consecutive_failures"]
        if failures >= self.MAX_FAILURES:
            self.state.update(enabled=False, next_check_at=None)
            self._set("paused_error", f"{reason} Paused after {failures} failures; inspect data and re-enable.")
        else:
            self._retry_at = now + timedelta(seconds=min(900, 60 * 2 ** (failures - 1)))
            self._due = self._retry_at
            self.state["next_check_at"] = self._due.isoformat()
            self._set("backoff", f"{reason} Retrying after {self._due.isoformat()}.")

    def tick(self):
        """One bounded decision; I/O outside control lock keeps pause responsive."""
        if not self._tick_lock.acquire(blocking=False):
            return
        generation = None
        try:
            now = self._clock()
            with self._lock:
                if not self.state["enabled"] or (self._due and now < self._due):
                    return
                generation = self._generation
                live = self.state["options"].get("scope") == "live"
                self._due = now + timedelta(seconds=self.LIVE_CHECK_SECONDS if live else self.CHECK_SECONDS)
                self.state.update(last_check_at=now.isoformat(), next_check_at=self._due.isoformat())
                options = copy.deepcopy(self.state["options"])
            jobs = self._jobs()
            # Never call snapshot() here: it re-evaluates every row's evidence.
            with jobs.lock:
                job = {key: jobs.state.get(key) for key in ("status", "started_at", "source", "evaluated")}
                job["has_errors"] = bool(jobs.state.get("errors"))
            with self._lock:
                if generation != self._generation or not self.state["enabled"]:
                    return
                if job["status"] == "running":
                    own = job["started_at"] == self._owned_scan and job["source"] == "automation"
                    self._set("scanning" if own else "waiting_scan",
                              "Automatic research scan is running." if own else "Waiting for the current manual or other scan.")
                    return
                if self._owned_scan is not None:
                    # A manual run may have replaced a just-finished automatic
                    # result before we observed it. Do not attribute its result.
                    own = job["started_at"] == self._owned_scan and job["source"] == "automation"
                    self._owned_scan = None
                    self._last_finish = now
                    if own and (job["status"] != "completed" or job["has_errors"] or not job["evaluated"]):
                        self._failed(now, "Automatic scan failed, had read errors, or evaluated no symbols.")
                        return
                    if own:
                        self._consumed = self._pending
                        self.state["consecutive_failures"] = 0
                        self._retry_at = None
                if not research_session_open(now):
                    self._warmup_started = None  # Closed sessions do not consume preparation time.
                    self._set("waiting_session", "Outside the configured weekday 09:15–15:20 IST window. No automatic scan.")
                    return
                if self._retry_at and now < self._retry_at:
                    self._set("backoff", "Waiting before a bounded retry.")
                    return
                if self._last_finish and now < self._last_finish + timedelta(seconds=self.state["min_scan_seconds"]):
                    self._set("cooldown", "Minimum delay after the last scan; overlapping or continuous scans are prevented.")
                    return
            if self._observe is None:
                from trading_agent.analysis.candle_changes import observe_candles
                observe = observe_candles
            else:
                observe = self._observe
            if self._symbols is not None:
                symbols = self._symbols()
            elif live:
                from trading_agent.analysis.research_engine import live_symbols
                symbols = live_symbols()
            else:
                from trading_agent.analysis.research_engine import universe
                symbols = universe()
            data = observe(symbols, options["interval"], now)
            # A readable risk ledger is mandatory, even for automatic research.
            # A blocked ledger may still be scanned: existing sizing gates keep
            # all plans at zero and readiness explains the risk block.
            ledger = self._ledger(now)
            fingerprint = (data["fingerprint"], ledger["fingerprint"])
            with self._lock:
                if generation != self._generation or not self.state["enabled"] or self._stop.is_set():
                    return
                self.state["data"] = {key: value for key, value in data.items() if key != "fingerprint"}
                self.state["data"]["observed_at"] = now.isoformat()
                self._warmup_started = None
                # Recheck time after potentially slow bootstrap I/O.
                finished = self._clock()
                if not research_session_open(finished):
                    self._set("waiting_session", "Entry session closed while checking stored data.")
                    return
                latest = data.get("latest_completed_at")
                from trading_agent.analysis.research_engine import INTERVAL_SECONDS
                fresh = latest and 0 <= (finished - datetime.fromisoformat(latest)).total_seconds() < (
                    INTERVAL_SECONDS[options["interval"]] * 2 + 15)
                if not data["fresh_symbols"] or not fresh:
                    self._set("waiting_data", "No fresh completed stored candles. Start/check the existing feed; no automatic downloads.")
                    return
                if fingerprint == self._consumed:
                    self._set("waiting_changes", "No new completed candles or paper-ledger changes since the last automatic scan.")
                    return
                try:
                    # The live scope scans exactly the symbols it observed.
                    result = jobs.start(dict(options, symbols=symbols) if live else options, source="automation")
                except RuntimeError:
                    self._set("waiting_scan", "Another scan owns the data directory; retrying without overlap.")
                    return
                self._pending = fingerprint
                self._owned_scan = result["started_at"]
                self.state.update(last_trigger_at=finished.isoformat(), scan_started_at=self._owned_scan)
                self._set("scanning", "Fresh data or changed paper ledger: scanning the "
                          f"{'live set' if live else 'full universe'} with evidence gates.")
        except CandleObservationPending as error:
            with self._lock:
                if generation == self._generation and self.state["enabled"]:
                    self._warmup_started = self._warmup_started or self._clock()
                    if (self._clock() - self._warmup_started).total_seconds() >= 600:
                        self._failed(self._clock(), "Stored-data preparation exceeded 10 minutes; review storage capacity.")
                    else:
                        self._set("preparing_data", f"Preparing stored-candle index ({error.cached_files} cached file summaries). "
                                  "No scan until the whole universe is observed; preparation is bounded to 10 minutes.")
        except Exception as error:
            # Never publish potentially sensitive provider/filesystem exception
            # text to the UI. No provider actions are attempted in this worker.
            with self._lock:
                if generation == self._generation and self.state["enabled"]:
                    self._failed(self._clock(), f"Stored-data/ledger check failed ({type(error).__name__}).")
        finally:
            self._tick_lock.release()


_automation = None
_init_lock = threading.Lock()


def get_research_automation():
    global _automation
    with _init_lock:
        if _automation is None:
            _automation = ResearchAutomation()
    return _automation
