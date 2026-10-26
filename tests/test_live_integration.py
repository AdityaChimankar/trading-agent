from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from trading_agent.ingest import live_ticker as module
from trading_agent.ingest.live_ticker import LiveTicker


@pytest.fixture
def ticker(monkeypatch):
    monkeypatch.setattr(module, "write_heartbeat", Mock())
    monkeypatch.setattr(module, "get_kite_auth", Mock())
    monkeypatch.setattr(module, "get_instrument_lookup", Mock())
    monkeypatch.setattr(module, "get_parquet_store", Mock())
    result = LiveTicker(["TEST"], interval="minute", on_candle=Mock())
    result._symbol_by_token = {1: "TEST"}
    return result


def tick(second, price=100, volume=1000):
    epoch = (datetime(2026, 10, 19, 3, 45, tzinfo=timezone.utc) + timedelta(seconds=second)).timestamp()
    return {"instrument_token": 1, "exchange_timestamp": datetime.fromtimestamp(epoch),
            "last_price": price, "volume_traded": volume, "depth": {"buy": [], "sell": []}}


def test_real_kite_messages_emit_only_completed_bars_with_volume_deltas(ticker):
    ticker._on_ticks(None, [tick(0), tick(30, 105, 1010)])
    ticker.on_candle.assert_not_called()
    ticker._on_ticks(None, [tick(60, 110, 1025)])
    candle = ticker.on_candle.call_args.args[0]
    assert (candle.open, candle.high, candle.low, candle.close, candle.volume) == (100, 105, 100, 105, 10)
    assert candle.timestamp == datetime(2026, 10, 19, 3, 45, tzinfo=timezone.utc)
    ticker._on_ticks(None, [tick(120, 100, 1030)])
    assert ticker.on_candle.call_args.args[0].volume == 15


def test_late_ticks_cannot_contaminate_current_candle(ticker):
    ticker._on_ticks(None, [tick(0), tick(60), tick(20, 500, 9999), tick(120)])
    assert ticker.on_candle.call_args.args[0].high == 100


def test_reconnect_callback_does_not_require_asyncio_loop(ticker):
    ticker._on_close(Mock(), 1006, "connection lost")


def test_scheduler_crons_use_exchange_timezone():
    from trading_agent.scripts.scheduler import scheduler
    for job in scheduler.get_jobs():
        assert str(job.trigger.timezone) == "Asia/Kolkata"


def test_scheduler_withholds_out_of_session_and_stale_inputs(monkeypatch):
    import pandas as pd
    from trading_agent.scripts import scheduler as module
    from trading_agent.storage import parquet_store
    monkeypatch.setattr(module, "in_market_hours", lambda: False)
    assert module._fresh_decision_inputs()[0] == []
    monkeypatch.setattr(module, "in_market_hours", lambda: True)
    monkeypatch.setattr(module, "get_watchlist_symbols", lambda: ["OLD", "FUTURE", "FRESH"])
    store = Mock()
    now = datetime.now(timezone.utc)
    store.read_latest.side_effect = lambda symbol, *a, **kw: pd.DataFrame({"timestamp": [
        now + timedelta(minutes={"OLD": -300, "FUTURE": 20, "FRESH": -6}[symbol])]})
    monkeypatch.setattr(parquet_store, "get_parquet_store", lambda: store)
    assert module._fresh_decision_inputs()[0] == ["FRESH"]


def test_heartbeat_is_shared_and_expired_state_is_not_healthy(tmp_path, monkeypatch):
    from trading_agent.ingest import heartbeat
    from types import SimpleNamespace
    import json
    monkeypatch.setattr(heartbeat, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    assert heartbeat.read_heartbeat() is None
    heartbeat.write_heartbeat(True, datetime.now(timezone.utc), 2)
    assert heartbeat.read_heartbeat()["connected"] is True
    data = heartbeat.read_heartbeat()
    data["updated_at"] = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    (tmp_path / "ticker-heartbeat.json").write_text(json.dumps(data))
    assert heartbeat.read_heartbeat()["connected"] is False


def test_idle_socket_heartbeat_preserves_actual_last_tick_and_reports_disconnect(ticker, monkeypatch):
    old_tick = datetime.now(timezone.utc) - timedelta(hours=2)
    ticker._last_tick = {1: old_tick}
    ticker._running = True
    ticker._ticker = Mock()
    ticker._ticker.is_connected.return_value = True
    clock = [100]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    ticker.publish_heartbeat()
    module.write_heartbeat.assert_called_once_with(True, old_tick, 0)
    clock[0] = 114
    ticker.publish_heartbeat()
    assert module.write_heartbeat.call_count == 1
    clock[0] = 116
    ticker._ticker.is_connected.return_value = False
    ticker.publish_heartbeat()
    module.write_heartbeat.assert_called_with(False, old_tick, 0)


def test_connect_without_new_ticks_does_not_erase_last_tick(ticker):
    old_tick = datetime.now(timezone.utc) - timedelta(hours=2)
    ticker._last_tick = {1: old_tick}
    ticker._on_connect(Mock(), {})
    module.write_heartbeat.assert_called_with(True, old_tick)


def _live_ticker(monkeypatch, minute_symbols, interval="5minute"):
    monkeypatch.setattr(module, "write_heartbeat", Mock())
    monkeypatch.setattr(module, "get_kite_auth", Mock())
    monkeypatch.setattr(module, "get_instrument_lookup", Mock())
    monkeypatch.setattr(module, "get_parquet_store", Mock())
    result = LiveTicker(["TEST", "OTHER"], interval=interval, on_candle=Mock(), minute_symbols=minute_symbols)
    result._symbol_by_token = {1: "TEST", 2: "OTHER"}
    return result


def _bars(ticker, interval):
    return [c.args[0] for c in ticker.on_candle.call_args_list if c.args[0].interval == interval]


def test_live_set_gets_completed_minute_bars_beside_the_live_interval(monkeypatch):
    ticker = _live_ticker(monkeypatch, ["TEST"])
    ticker._on_ticks(None, [tick(10), tick(70, 101, 1010), tick(100, 103, 1020), tick(130, 102, 1030)])
    minute = _bars(ticker, "minute")
    # The first minute after connecting missed earlier ticks: never written.
    assert [(b.timestamp.minute, b.open, b.high, b.low, b.close, b.volume) for b in minute] == [(46, 101, 103, 101, 103, 20)]
    assert minute[0].symbol == "TEST" and _bars(ticker, "5minute") == []
    ticker._on_ticks(None, [tick(300, 104, 1040)])
    assert [b.timestamp.minute for b in _bars(ticker, "minute")] == [46, 47]
    assert len(_bars(ticker, "5minute")) == 1


def test_reconnect_restarts_the_partial_minute_and_other_symbols_get_none(monkeypatch):
    ticker = _live_ticker(monkeypatch, ["TEST"])
    other = dict(tick(10), instrument_token=2)
    ticker._on_ticks(None, [tick(10), other, tick(70, 101, 1010), dict(tick(70), instrument_token=2)])
    assert [b.symbol for b in _bars(ticker, "minute")] == []  # TEST's first minute was partial
    ws = Mock()
    ticker._on_connect(ws, None)
    ticker._on_ticks(None, [tick(130, 102, 1030), tick(190, 103, 1040), tick(250, 104, 1050)])
    assert [b.timestamp.minute for b in _bars(ticker, "minute")] == [48]  # 47 restarted partial; 46 dropped


def test_a_minute_feed_does_not_build_a_second_minute_series(monkeypatch):
    ticker = _live_ticker(monkeypatch, ["TEST"], interval="minute")
    assert ticker.minute_symbols == set()
