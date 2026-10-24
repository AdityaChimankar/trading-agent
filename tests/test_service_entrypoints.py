"""Exercise production entry points with provider calls replaced by fakes."""
import signal
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from trading_agent.scripts import scheduler as scheduled
from trading_agent.ingest import live_ticker


def handlers(monkeypatch):
    installed = {}
    originals = {signal.SIGINT: object(), signal.SIGTERM: object()}

    def register(sig, handler):
        previous = installed.get(sig, originals[sig])
        installed[sig] = handler
        return previous

    monkeypatch.setattr(signal, "signal", register)
    return installed, originals


def test_scheduler_entrypoint_starts_without_provider_work_and_stops_on_sigterm(monkeypatch):
    installed, originals = handlers(monkeypatch)
    runner = Mock(running=True)
    runner.start.side_effect = lambda: installed[signal.SIGTERM](signal.SIGTERM, None)
    monkeypatch.setattr(scheduled, "scheduler", runner)
    startup = Mock(side_effect=AssertionError("No implicit startup provider pass"))
    monkeypatch.setattr(scheduled, "run_once_now", startup)
    monkeypatch.setattr("trading_agent.core.logging_config.setup_logging", lambda: None)
    with pytest.raises(SystemExit) as error:
        scheduled.main()
    assert error.value.code == 0
    runner.start.assert_called_once()
    runner.shutdown.assert_called_once_with(wait=True)
    startup.assert_not_called()
    assert installed == originals


def test_scheduler_failed_start_restores_signal_handlers(monkeypatch):
    installed, originals = handlers(monkeypatch)
    runner = Mock(running=False)
    runner.start.side_effect = RuntimeError("start failed")
    monkeypatch.setattr(scheduled, "scheduler", runner)
    monkeypatch.setattr("trading_agent.core.logging_config.setup_logging", lambda: None)
    with pytest.raises(RuntimeError, match="start failed"):
        scheduled.main()
    runner.shutdown.assert_not_called()
    assert installed == originals


def test_ticker_entrypoint_publishes_heartbeat_without_ticks_and_stops(monkeypatch):
    installed, originals = handlers(monkeypatch)
    ticker = Mock(_running=True)
    monkeypatch.setattr(live_ticker, "LiveTicker", lambda **kwargs: ticker)
    monkeypatch.setattr(live_ticker, "get_kite_auth", lambda: SimpleNamespace(
        credentials=SimpleNamespace(access_token="test-only")))
    monkeypatch.setattr(live_ticker, "get_settings", lambda: SimpleNamespace(
        storage=SimpleNamespace(candle_interval="minute")))
    monkeypatch.setattr(live_ticker, "create_default_candle_handler", lambda: None)
    monkeypatch.setattr("trading_agent.ingest.watchlist_resolved.get_watchlist_symbols", lambda: ["TEST"])
    monkeypatch.setattr("trading_agent.core.logging_config.setup_logging", lambda: None)
    monkeypatch.setattr(live_ticker.time, "sleep", lambda seconds: installed[signal.SIGTERM](signal.SIGTERM, None))
    with pytest.raises(SystemExit):
        live_ticker.main()
    ticker.start.assert_called_once()
    ticker.publish_heartbeat.assert_called_once()
    ticker.stop.assert_called_once()
    assert installed == originals


@pytest.mark.parametrize("state,repair", [("stale", True), ("missing", True), ("fresh", False), ("closed_session", False)])
def test_feed_watchdog_distinguishes_socket_health_from_tick_freshness(monkeypatch, state, repair):
    stamp = datetime.now(UTC) - timedelta(minutes=30 if state == "stale" else 1)
    heartbeat = None if state == "missing" else {"connected": True, "last_tick": stamp.isoformat()}
    monkeypatch.setattr("trading_agent.core.freshness.ingest_heartbeat", lambda: heartbeat)
    monkeypatch.setattr(scheduled, "in_market_hours", lambda: state != "closed_session")
    monkeypatch.setattr("trading_agent.core.config.get_settings", lambda: SimpleNamespace(
        storage=SimpleNamespace(candle_interval="minute")))
    heal = Mock()
    monkeypatch.setattr(scheduled, "end_of_session_heal", heal)
    scheduled.heal_if_feed_down()
    assert heal.called is repair
