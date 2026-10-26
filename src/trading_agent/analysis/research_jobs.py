"""One bounded background research scan for the single-worker local deployment."""
import copy
import json
import threading
from datetime import UTC, datetime
from uuid import uuid4
from filelock import FileLock, Timeout

from trading_agent.analysis.research_engine import analyze_symbol, model_for_research, size_opportunities, universe, refresh_eligibility
from trading_agent.analysis.outcome_validation import estimate
from trading_agent.core.config import get_settings
from trading_agent.risk.ledger_guard import read_ledger_state


class ResearchJobs:
    def __init__(self):
        self.lock = threading.RLock()
        self.cancelled = threading.Event()
        self.state = {"status": "idle", "processed": 0, "total": 0, "rows": [], "errors": [], "skipped": []}
        # The last completed scan, shown by the classic dashboard while a new one runs.
        self.previous = None
        self.path = get_settings().data_dir / "research" / "latest_scan.json"
        self.run_lock = FileLock(str(self.path) + ".run.lock", timeout=0, thread_local=False)
        if self.path.exists():
            try:
                self.state = json.loads(self.path.read_text(encoding="utf-8"))
                if self.state["status"] == "running":
                    self.state["status"] = "interrupted"
            except (ValueError, KeyError):
                pass

    def snapshot(self, completed=False):
        """The current scan; with ``completed``, the last finished one while a scan runs."""
        with self.lock:
            running = self.state["status"] == "running"
            state = copy.deepcopy(self.previous if completed and running and self.previous else self.state)
        now = datetime.now(UTC)
        allocation_warning = None
        try:
            ledger = read_ledger_state(now)
            if not ledger["entries_allowed"]:
                allocation_warning = ledger["block_reason"]
            elif state["rows"] and state.get("ledger_fingerprint") != ledger["fingerprint"]:
                allocation_warning = "Paper ledger, session or limits changed; re-scan to recompute allocations"
        except Exception:
            allocation_warning = "Paper ledger cannot be verified; allocations blocked until a successful re-scan"
        state["allocation_warning"] = allocation_warning
        for row in state["rows"]:
            refresh_eligibility(row, now)
            if row.get("validation", {}).get("status") == "qualified_research":
                try:
                    validation = estimate(row, state.get("options", {}).get("mode"),
                                          state.get("options", {}).get("reward_risk", 2), now)
                    if not isinstance(validation, dict) or not isinstance(validation.get("status"), str):
                        raise ValueError("Invalid outcome evidence result")
                    row["validation"] = validation
                except Exception:
                    # A failed evidence read must remove the saved qualification,
                    # without exposing filesystem paths or exception contents.
                    row["validation"] = {"status": "unavailable", "win_probability": None,
                                         "expected_net_return_pct": None,
                                         "reason": "Outcome evidence could not be verified"}
                if state.get("options", {}).get("require_validated", True) and row["validation"]["status"] != "qualified_research" and row.get("plan"):
                    row["plan"].update(blocked=True, block_reason="Outcome evidence no longer qualified",
                                       quantity=0, position_value=0, risk_amount=0)
            # Old snapshots predate the evidence contract: never promote them.
            if "validation" not in row and row.get("plan"):
                row["plan"].update(blocked=True, block_reason="Legacy snapshot; rescan for validation gates",
                                   quantity=0, position_value=0, risk_amount=0)
            if allocation_warning and row.get("plan"):
                row["plan"].update(blocked=True, block_reason=allocation_warning,
                                   quantity=0, position_value=0, risk_amount=0)
        state["eligible"] = sum(bool(r.get("plan")) and not r["plan"]["blocked"] for r in state["rows"])
        state["stale"] = sum(r.get("stale", False) for r in state["rows"])
        return state

    def save(self):
        # Lazy import avoids eagerly constructing the FastAPI application when
        # this service is imported directly by the CLI or tests.
        from trading_agent.api.serializers import jsonable
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".scan-{uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(jsonable(self.state), allow_nan=False), encoding="utf-8")
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def start(self, options, *, source="manual"):
        if source not in {"manual", "automation"}:
            raise ValueError("Scan source must be manual or automation")
        options = copy.deepcopy(options)
        with self.lock:
            if self.state["status"] == "running":
                raise RuntimeError("A research scan is already running")
            symbols = options.pop("symbols", None)
            symbols = sorted(set(symbols)) if symbols is not None else universe()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.run_lock.acquire()
            except Timeout as error:
                raise RuntimeError("Another process is scanning this data directory") from error
            self.cancelled.clear()
            if self.state.get("status") == "completed":
                self.previous = copy.deepcopy(self.state)
            self.state = {"status": "running", "processed": 0, "total": len(symbols), "rows": [],
                          "errors": [], "skipped": [], "started_at": datetime.now(UTC).isoformat(),
                          "options": options, "source": source}
            try:
                self.save()
                threading.Thread(target=self.run, args=(symbols, options), daemon=True, name="research-scan").start()
            except Exception:
                self.state["status"] = "error"
                self.state["errors"].append({"symbol": None, "reason": "Scan could not start"})
                self.run_lock.release()
                raise
            return self.snapshot()

    def run(self, symbols, options):
        try:
            # Validate the ledger before scanning. Re-read it for final sizing so
            # positions and realized equity come from the same SQLite snapshot.
            read_ledger_state()
            model = model_for_research()
            now = datetime.now(UTC)
            rows = []
            with self.lock:
                self.state.update(model_status=model.status, model_warning=model.warning)
            for symbol in symbols:
                if self.cancelled.is_set():
                    break
                try:
                    row, _ = analyze_symbol(symbol, options["interval"], options["mode"], model, now)
                    row["validation"] = estimate(row, options["mode"], options["reward_risk"], datetime.now(UTC))
                    rows.append(row)
                except ValueError as error:
                    with self.lock:
                        self.state["skipped"].append({"symbol": symbol, "reason": str(error)})
                except Exception as error:
                    with self.lock:
                        self.state["errors"].append({"symbol": symbol, "reason": type(error).__name__})
                with self.lock:
                    self.state["processed"] += 1
                    self.state["current_symbol"] = symbol
                    if self.state["processed"] % 25 == 0:
                        self.save()
            # A long scan must not use its start time to authorize old signals.
            for row in rows:
                refresh_eligibility(row, datetime.now(UTC))
            ledger = read_ledger_state()
            capital = max(0, min(options["capital"], ledger["realized_equity"]))
            sized = size_opportunities(rows, capital, ledger["positions"], get_settings().paper,
                                       options["risk_pct"], options["reward_risk"], options.get("require_validated", True),
                                       daily_risk_headroom=ledger["daily_risk_headroom"], tick_size=_tick_size)
            if not ledger["entries_allowed"]:
                for row in sized:
                    row["plan"].update(blocked=True, block_reason=ledger["block_reason"],
                                       quantity=0, position_value=0, risk_amount=0)
            with self.lock:
                self.state.update(rows=sized, status="cancelled" if self.cancelled.is_set() else "completed",
                                  completed_at=datetime.now(UTC).isoformat(),
                                  effective_capital=capital, ledger_fingerprint=ledger["fingerprint"],
                                  allocation_warning=ledger["block_reason"],
                                  evaluated=len(rows), stale=sum(row["stale"] for row in rows),
                                  eligible=sum(not row["plan"]["blocked"] for row in sized))
                self.save()
        except Exception as error:
            with self.lock:
                self.state["status"] = "error"
                self.state["errors"].append({"symbol": None, "reason": type(error).__name__})
                self.save()
        finally:
            self.run_lock.release()


def _tick_size(symbol):
    """Tick from the cached Kite instrument dump; None (sizing assumes 0.05) when unknown."""
    from trading_agent.ingest.instrument_lookup import get_instrument_lookup

    try:
        return get_instrument_lookup().get_tick_size(symbol)
    except Exception:
        return None


_jobs = None
_init_lock = threading.Lock()


def get_research_jobs():
    global _jobs
    with _init_lock:
        if _jobs is None:
            _jobs = ResearchJobs()
    return _jobs


if __name__ == "__main__":
    import argparse
    import time
    parser = argparse.ArgumentParser(description="Scan the full stored-candle watchlist without broker or AI requests.")
    parser.add_argument("--interval", choices=["minute", "5minute", "15minute", "day"], default="minute")
    parser.add_argument("--capital", type=float, default=100000)
    parser.add_argument("--mode", choices=["rule_only", "ml_only", "rule_ml_combined"], default="rule_ml_combined")
    parser.add_argument("--exploratory", action="store_true", help="Allow research plans without qualified evidence")
    args = parser.parse_args()
    if not 0 < args.capital < float("inf"):
        parser.error("capital must be finite and positive")
    job = get_research_jobs()
    job.start({"capital": args.capital, "interval": args.interval, "mode": args.mode,
               "risk_pct": 1.0, "reward_risk": 2.0, "require_validated": not args.exploratory})
    try:
        while job.snapshot()["status"] == "running":
            time.sleep(5)
            snapshot = job.snapshot()
            print({key: snapshot.get(key) for key in ("status", "processed", "total", "current_symbol")}, flush=True)
    except KeyboardInterrupt:
        job.cancelled.set()
        while job.snapshot()["status"] == "running":
            time.sleep(0.1)
    snapshot = job.snapshot()
    print({key: snapshot.get(key) for key in ("status", "processed", "total", "evaluated", "stale", "eligible")})
    print("Skipped:", len(snapshot["skipped"]), "Errors:", len(snapshot["errors"]))
    raise SystemExit(0 if snapshot["status"] == "completed" and not snapshot["errors"] else 1)
