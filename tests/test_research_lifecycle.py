"""API startup/shutdown must never silently arm stored-data automation."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import threading

import pytest

from trading_agent.analysis import research_automation
from trading_agent.analysis.research_automation import ResearchAutomation
from trading_agent.api import main
from trading_agent.storage import db


@pytest.fixture
def lifecycle(monkeypatch):
    def forbidden(*args):
        raise AssertionError("Disabled automation must not inspect data or start scans")

    worker = ResearchAutomation(jobs=forbidden, observe=forbidden, ledger=forbidden)
    database = SimpleNamespace(create_tables=AsyncMock(), close=AsyncMock())
    cleanup = SimpleNamespace(register_signals=Mock(side_effect=AssertionError("ASGI host owns signals")),
                              run_cleanup=AsyncMock())
    settings = SimpleNamespace(api=SimpleNamespace(workers=1, research_autostart="off"))
    monkeypatch.setattr(research_automation, "get_research_automation", lambda: worker)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "get_shutdown_manager", lambda: cleanup)
    monkeypatch.setattr(main, "setup_logging", lambda: None)
    monkeypatch.setattr(db, "get_database", lambda: database)
    return worker, database, cleanup, settings


@pytest.mark.asyncio
async def test_restart_requires_rearming_and_exception_still_cleans_up(lifecycle):
    worker, database, cleanup, _ = lifecycle
    worker.enable({"require_validated": True, "interval": "minute"})
    with pytest.raises(RuntimeError, match="simulated request"):
        async with main.lifespan(None):
            assert worker.snapshot()["running"]
            assert not worker.snapshot()["enabled"]
            raise RuntimeError("simulated request")
    assert not worker.snapshot()["running"]
    assert not worker.snapshot()["enabled"]
    database.close.assert_awaited_once()
    cleanup.run_cleanup.assert_awaited_once()
    async with main.lifespan(None):
        assert worker.snapshot()["running"]
        assert not worker.snapshot()["enabled"]
    assert database.close.await_count == 2


@pytest.mark.asyncio
async def test_multiple_workers_do_not_start_automatic_research(lifecycle):
    worker, _, _, settings = lifecycle
    settings.api.workers = 2
    async with main.lifespan(None):
        assert not worker.snapshot()["running"]
        assert not worker.snapshot()["enabled"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["database", "thread"])
async def test_failed_startup_still_closes_database(lifecycle, monkeypatch, failure):
    worker, database, cleanup, _ = lifecycle
    if failure == "database":
        database.create_tables.side_effect = RuntimeError("database startup failed")
    else:
        def fail_start(self):
            raise RuntimeError("thread startup failed")
        monkeypatch.setattr(threading.Thread, "start", fail_start)
    with pytest.raises(RuntimeError, match=f"{failure} startup failed"):
        async with main.lifespan(None):
            pytest.fail("Startup failure must not yield a running API")
    assert not worker.snapshot()["running"]
    database.close.assert_awaited_once()
    cleanup.run_cleanup.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_cleanup_still_closes_database(lifecycle):
    worker, database, cleanup, _ = lifecycle
    cleanup.run_cleanup.side_effect = RuntimeError("cleanup failed")
    with pytest.raises(RuntimeError, match="cleanup failed"):
        async with main.lifespan(None):
            assert worker.snapshot()["running"]
    assert not worker.snapshot()["running"]
    database.close.assert_awaited_once()


def test_restart_rejects_worker_still_finishing_a_blocked_observation(monkeypatch):
    worker = ResearchAutomation()
    entered, release = threading.Event(), threading.Event()

    def blocked_tick():
        entered.set()
        release.wait(timeout=5)

    monkeypatch.setattr(worker, "tick", blocked_tick)
    worker.start_worker()
    old_thread = worker._thread
    try:
        assert entered.wait(timeout=2)
        # Simulate the bounded join timing out while observation I/O is pending.
        with monkeypatch.context() as patch:
            patch.setattr(old_thread, "join", lambda timeout: None)
            worker.stop_worker()
        assert old_thread.is_alive()
        assert not worker.snapshot()["running"]
        with pytest.raises(RuntimeError, match="still stopping"):
            worker.start_worker()
    finally:
        release.set()
        old_thread.join(timeout=2)
    assert not old_thread.is_alive()
    try:
        worker.start_worker()
        assert worker.snapshot()["running"]
        assert not worker.snapshot()["enabled"]
    finally:
        worker.stop_worker()
