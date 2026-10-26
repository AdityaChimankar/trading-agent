"""Offline autonomy tests: no providers, real ledger writes or market scans."""
import copy
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from trading_agent.analysis.research_automation import ResearchAutomation, research_session_open
from trading_agent.api.routers import research

NOW = datetime(2026, 10, 21, 5, tzinfo=UTC)
OPTIONS = {"capital": 100000, "interval": "minute", "mode": "rule_only",
           "risk_pct": 1, "reward_risk": 2, "require_validated": True}


class Jobs:
    def __init__(self):
        self.lock = threading.RLock()
        self.cancelled = threading.Event()
        self.state = {"status": "idle"}
        self.calls = []

    def start(self, options, *, source="manual"):
        if self.state["status"] == "running":
            raise RuntimeError("busy")
        self.calls.append(copy.deepcopy(options))
        self.state = {"status": "running", "started_at": f"scan-{len(self.calls)}",
                      "source": source, "errors": [], "evaluated": 0}
        return self.state

    def complete(self, **updates):
        self.state.update(status="completed", evaluated=2, **updates)


@pytest.fixture
def setup():
    clock = SimpleNamespace(now=NOW)
    data = {"fingerprint": "bars-1", "fresh_symbols": 2, "stored_symbols": 2,
            "configured_symbols": 3}
    ledger = {"fingerprint": "book-1"}
    observations = []
    jobs = Jobs()

    def observe(symbols, interval, now):
        observations.append((symbols, interval))
        return dict(data, latest_completed_at=(now - timedelta(minutes=1)).isoformat())

    worker = ResearchAutomation(jobs=lambda: jobs, observe=observe, ledger=lambda _: ledger,
                                clock=lambda: clock.now, symbols=lambda: ["AAA", "BBB", "CCC"])
    return worker, jobs, clock, data, ledger, observations


def advance(setup, seconds=30):
    worker, _, clock, *_ = setup
    clock.now += timedelta(seconds=seconds)
    worker.tick()


def test_default_disabled_and_restarts_require_explicit_enable(setup):
    worker, jobs, *_ = setup
    worker.tick()
    assert not jobs.calls
    assert not worker.snapshot()["enabled"]
    worker.enable(OPTIONS)
    assert not any(worker.snapshot()["capabilities"].values())
    assert not ResearchAutomation().snapshot()["enabled"]
    snapshot = worker.snapshot()
    snapshot["options"]["capital"] = 1
    assert worker.snapshot()["options"]["capital"] == 100000


def test_new_data_full_universe_cooldown_no_overlap_and_ledger_changes(setup):
    worker, jobs, _, data, ledger, observations = setup
    worker.enable(OPTIONS, 60)
    worker.tick()
    assert len(jobs.calls) == 1
    assert observations == [(["AAA", "BBB", "CCC"], "minute")]
    assert jobs.calls[0] == OPTIONS
    advance(setup)
    assert worker.snapshot()["status"] == "scanning"
    assert len(observations) == 1
    jobs.complete()
    advance(setup)
    assert worker.snapshot()["status"] == "cooldown"
    advance(setup, 60)
    assert worker.snapshot()["status"] == "waiting_changes"
    assert len(jobs.calls) == 1
    ledger["fingerprint"] = "book-2"
    advance(setup)
    assert len(jobs.calls) == 2
    # Data arriving DURING this run must not be marked as already consumed.
    data["fingerprint"] = "bars-2"
    jobs.complete()
    advance(setup)
    advance(setup, 60)
    assert len(jobs.calls) == 3


def test_cooldown_measured_after_completion_not_start(setup):
    worker, jobs, clock, *_ = setup
    worker.enable(OPTIONS, 120)
    worker.tick()
    clock.now += timedelta(minutes=5)
    jobs.complete()
    worker.tick()
    assert worker.snapshot()["status"] == "cooldown"
    assert len(jobs.calls) == 1


def test_wait_for_manual_scan_and_pause_never_cancels_manual_work(setup):
    worker, jobs, *_ = setup
    jobs.start(OPTIONS)
    worker.enable(OPTIONS)
    worker.tick()
    assert worker.snapshot()["status"] == "waiting_scan"
    worker.pause()
    assert not jobs.cancelled.is_set()
    assert len(jobs.calls) == 1


def test_pause_cancels_only_owned_automatic_scan(setup):
    worker, jobs, *_ = setup
    worker.enable(OPTIONS)
    worker.tick()
    worker.pause()
    assert jobs.cancelled.is_set()
    advance(setup, 300)
    assert len(jobs.calls) == 1
    assert not worker.snapshot()["enabled"]


def test_no_auto_scan_with_stale_data_or_outside_session(setup):
    worker, jobs, clock, data, _, observations = setup
    worker.enable(OPTIONS)
    clock.now = NOW.replace(hour=12)
    worker.tick()
    assert worker.snapshot()["status"] == "waiting_session"
    assert not observations
    clock.now = NOW + timedelta(days=1)
    data["fresh_symbols"] = 0
    worker.tick()
    assert worker.snapshot()["status"] == "waiting_data"
    assert not jobs.calls


@pytest.mark.parametrize("stamp,expected", [
    (datetime(2026, 10, 21, 3, 44, tzinfo=UTC), False),
    (datetime(2026, 10, 21, 3, 45, tzinfo=UTC), True),
    (datetime(2026, 10, 21, 9, 50, tzinfo=UTC), False),
    (datetime(2026, 10, 24, 5, tzinfo=UTC), False),
])
def test_configured_session_window(stamp, expected):
    assert research_session_open(stamp) is expected


def test_repeated_observation_failures_backoff_and_disarm(setup):
    worker, jobs, _, *_ = setup

    def failed(*_):
        raise OSError("SECRET must not be returned")

    worker._observe = failed
    worker.enable(OPTIONS)
    worker.tick()
    assert worker.snapshot()["status"] == "backoff"
    advance(setup, 30)
    assert worker.snapshot()["consecutive_failures"] == 1
    advance(setup, 30)
    assert worker.snapshot()["consecutive_failures"] == 2
    advance(setup, 120)
    status = worker.snapshot()
    assert status["status"] == "paused_error"
    assert not status["enabled"]
    assert status["next_check_at"] is None
    assert "SECRET" not in str(status)
    assert not jobs.calls


def test_cold_index_progress_waits_without_partial_scan_then_caps_preparation(setup):
    from trading_agent.analysis.candle_changes import CandleObservationPending
    worker, jobs, *_ = setup

    def pending(*_):
        raise CandleObservationPending(1000)

    worker._observe = pending
    worker.enable(OPTIONS)
    worker.tick()
    for _ in range(3):
        advance(setup)
    assert worker.snapshot()["status"] == "preparing_data"
    assert worker.snapshot()["consecutive_failures"] == 0
    assert worker.snapshot()["data"] is None
    assert not jobs.calls
    advance(setup, 600)
    assert worker.snapshot()["status"] == "backoff"
    assert worker.snapshot()["consecutive_failures"] == 1


def test_repeated_failed_scans_cannot_storm(setup):
    worker, jobs, *_ = setup
    worker.enable(OPTIONS, 60)
    worker.tick()
    for attempt in range(3):
        jobs.state["status"] = "error"
        advance(setup)
        if attempt < 2:
            assert worker.snapshot()["status"] == "backoff"
            advance(setup, 60 * 2 ** attempt)
    assert len(jobs.calls) == 3
    assert worker.snapshot()["status"] == "paused_error"


def test_empty_or_partial_error_scan_is_not_consumed_as_success(setup):
    worker, jobs, *_ = setup
    worker.enable(OPTIONS)
    worker.tick()
    jobs.complete(errors=[{"reason": "OSError"}])
    advance(setup)
    assert worker.snapshot()["consecutive_failures"] == 1
    assert worker._consumed is None


def test_fail_closed_when_ledger_unavailable(setup):
    worker, jobs, *_ = setup

    def failed(_):
        raise OSError("ledger missing")

    worker._ledger = failed
    worker.enable(OPTIONS)
    worker.tick()
    assert worker.snapshot()["status"] == "backoff"
    assert not jobs.calls


def test_cross_process_lock_contention_waits_instead_of_counting_failure(setup):
    worker, jobs, *_ = setup

    def busy(*args, **kwargs):
        raise RuntimeError("Another process is scanning")

    jobs.start = busy
    worker.enable(OPTIONS)
    worker.tick()
    assert worker.snapshot()["status"] == "waiting_scan"
    assert worker.snapshot()["consecutive_failures"] == 0


def test_pause_during_observation_cannot_launch_late_scan(setup):
    worker, jobs, *_ = setup
    entered, release = threading.Event(), threading.Event()
    observe = worker._observe

    def slow(*args):
        entered.set()
        assert release.wait(timeout=5)
        return observe(*args)

    worker._observe = slow
    worker.enable(OPTIONS)
    thread = threading.Thread(target=worker.tick)
    thread.start()
    try:
        assert entered.wait(timeout=5)
        worker.pause()
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert not jobs.calls
    assert not worker.snapshot()["enabled"]


def test_slow_observation_cannot_scan_after_candles_expire(setup):
    worker, jobs, clock, *_ = setup
    observe = worker._observe

    def slow(*args):
        data = observe(*args)
        clock.now += timedelta(minutes=4)
        return data

    worker._observe = slow
    worker.enable(OPTIONS)
    worker.tick()
    assert worker.snapshot()["status"] == "waiting_data"
    assert not jobs.calls


def test_settings_are_immutable_until_paused_and_restricted(setup):
    worker, *_ = setup
    worker.enable(OPTIONS)
    with pytest.raises(RuntimeError, match="Pause"):
        worker.enable(OPTIONS)
    worker.pause()
    for updates in ({"require_validated": False}, {"symbols": ["AAA"]}, {"interval": "day"}):
        with pytest.raises(ValueError):
            worker.enable(dict(OPTIONS, **updates))


def test_worker_starts_disabled_stops_and_cannot_start_scans_after_shutdown(setup):
    worker, jobs, *_ = setup
    worker.start_worker()
    assert worker.snapshot()["running"]
    worker.stop_worker()
    assert not worker.snapshot()["running"]
    assert not worker.snapshot()["enabled"]
    worker.enable(OPTIONS)  # Direct misuse cannot override the shutdown event.
    worker.tick()
    assert not jobs.calls


def test_api_validates_automatic_settings_and_worker(monkeypatch, setup):
    from trading_agent.analysis import research_automation as module
    worker, jobs, *_ = setup
    monkeypatch.setattr(module, "get_research_automation", lambda: worker)
    monkeypatch.setattr(research, "get_research_jobs", lambda: jobs)
    settings = SimpleNamespace(api=SimpleNamespace(workers=1))
    monkeypatch.setattr(research, "get_settings", lambda: settings)
    app = FastAPI()
    app.include_router(research.router)
    with TestClient(app) as client:
        assert client.get("/automation").json()["enabled"] is False
        assert client.post("/automation/enable", json={"scan": OPTIONS}).status_code == 409
        worker._thread = SimpleNamespace(is_alive=lambda: True)
        for updates in ({"require_validated": False}, {"symbols": ["AAA"]},
                        {"interval": "day"}, {"capital": -1}, {"risk_pct": 3}):
            response = client.post("/automation/enable", json={"scan": dict(OPTIONS, **updates)})
            assert response.status_code == 422
        assert client.post("/automation/enable", json={"scan": OPTIONS, "min_scan_seconds": 0}).status_code == 422
        settings.api.workers = 2
        assert client.post("/automation/enable", json={"scan": OPTIONS}).status_code == 409
        settings.api.workers = 1
        assert client.post("/automation/enable", json={"scan": OPTIONS}).json()["enabled"] is True
        assert client.post("/automation/enable", json={"scan": OPTIONS}).status_code == 409
        assert client.post("/automation/pause").json()["enabled"] is False
        assert client.post("/automation/enable", json={"scan": OPTIONS}).status_code == 200
        client.post("/scan/cancel")
        assert not worker.snapshot()["enabled"]
        assert jobs.cancelled.is_set()


def test_live_scope_observes_and_scans_only_the_live_set_every_10_seconds(setup, monkeypatch):
    _, jobs, clock, data, ledger, observations = setup
    monkeypatch.setattr("trading_agent.analysis.research_engine.live_symbols", lambda: ["AAA", "BBB"])
    worker = ResearchAutomation(jobs=lambda: jobs, observe=lambda symbols, interval, now: observations.append(
        (symbols, interval)) or dict(data, latest_completed_at=(now - timedelta(minutes=1)).isoformat()),
        ledger=lambda _: ledger, clock=lambda: clock.now)
    worker.enable(dict(OPTIONS, scope="live"), 60)
    worker.tick()
    assert observations == [(["AAA", "BBB"], "minute")]
    assert jobs.calls == [dict(OPTIONS, scope="live", symbols=["AAA", "BBB"])]
    assert worker.snapshot()["next_check_at"] == (NOW + timedelta(seconds=10)).isoformat()
    assert "live set" in worker.snapshot()["reason"]


def test_live_scope_needs_a_live_set_and_scopes_are_restricted(setup, monkeypatch):
    worker, *_ = setup
    with pytest.raises(ValueError):
        worker.enable(dict(OPTIONS, scope="handpicked"))
    monkeypatch.setattr("trading_agent.analysis.research_engine.live_symbols", lambda: [])
    with pytest.raises(ValueError):
        worker.enable(dict(OPTIONS, scope="live"))


def test_api_can_arm_the_live_scope_at_startup(tmp_path, monkeypatch):
    from trading_agent.analysis import research_automation
    from trading_agent.api.main import create_app
    from trading_agent.core.config import get_settings

    monkeypatch.setenv("API_RESEARCH_AUTOSTART", "live")
    monkeypatch.setenv("API_AUTH_TOKEN", "")
    monkeypatch.setattr(research_automation, "_automation", None)
    monkeypatch.setattr(ResearchAutomation, "tick", lambda self: None)  # no stored-data checks here
    monkeypatch.setattr("trading_agent.analysis.research_engine.live_symbols", lambda: ["AAA"])
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            state = client.get("/api/v1/research/automation").json()
        assert state["enabled"] and state["options"]["scope"] == "live"
        assert state["options"]["require_validated"] is True and state["min_scan_seconds"] == 60
    finally:
        get_settings.cache_clear()
