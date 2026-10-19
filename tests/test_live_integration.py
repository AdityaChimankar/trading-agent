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
