"""Exercise the API routes consumed by the operating console on a fresh ledger."""
import asyncio
from pathlib import Path
import subprocess
import os
import sys

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from trading_agent.api.main import create_app
from trading_agent.core.config import get_settings
from trading_agent.storage import db as db_module, parquet_store as parquet_module


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "ledger.db"
    monkeypatch.setenv("DB_URL", f"sqlite:///{path.as_posix()}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PAPER_INITIAL_CAPITAL", "1000000")
    monkeypatch.setenv("API_AUTH_TOKEN", "")
    monkeypatch.setenv("PAPER_ENTRIES_ENABLED", "true")
    monkeypatch.setattr(db_module, "DB_PATH", str(path))
    monkeypatch.setattr(parquet_module, "_store", parquet_module.ParquetStore(tmp_path / "parquet"))
    get_settings.cache_clear()
    db_module.reset_database()
    try:
        with TestClient(create_app()) as instance:
            yield instance
    finally:
        db_module.reset_database()
        get_settings.cache_clear()


VALID = {"symbol": "TEST", "action": "BUY", "entry_price": 100, "quantity": 10,
         "stop_loss": 95, "take_profit": 110}


def test_positions_and_wallet_share_persistent_ledger(client):
    created = client.post("/api/v1/positions/", json=VALID)
    assert created.status_code == 200, created.text
    snapshot = client.get("/api/v1/wallet/summary").json()
    assert snapshot["cash"] == 999000
    assert snapshot["invested"] == 1000
    assert snapshot["open_positions"] == 1
    from trading_agent.risk.portfolio_risk import get_open_positions
    assert get_open_positions()[0]["symbol"] == "TEST"
    assert client.post("/api/v1/positions/", json=VALID).status_code == 409
    closed = client.post(f"/api/v1/positions/{created.json()['id']}/close?exit_price=105")
    assert closed.status_code == 200
    assert client.get("/api/v1/wallet/summary").json()["cash"] == 1000050
    metrics = client.get("/api/v1/wallet/metrics")
    assert metrics.status_code == 200
    assert metrics.json()["total_pnl"] == 50
    assert metrics.json()["profit_factor"] is None
    assert len(client.get("/api/v1/wallet/trades").json()) == 1
    assert client.post("/api/v1/wallet/reset").status_code == 409


@pytest.mark.parametrize("change", [{"quantity": 0}, {"quantity": 1.5}, {"action": "HOLD"},
                                    {"entry_price": -1}, {"stop_loss": 105}, {"take_profit": 90}])
def test_invalid_entries_never_reach_ledger(client, change):
    assert client.post("/api/v1/positions/", json=VALID | change).status_code == 422
    assert client.get("/api/v1/positions/").json() == []


def test_break_even_close_does_not_break_metrics(client):
    created = client.post("/api/v1/positions/", json=VALID).json()
    assert client.post(f"/api/v1/positions/{created['id']}/close?exit_price=100").status_code == 200
    metrics = client.get("/api/v1/wallet/metrics")
    assert metrics.status_code == 200
    assert metrics.json()["losing_trades"] == 0


def test_risk_limit_and_entry_switch_fail_closed(client, monkeypatch):
    assert client.post("/api/v1/positions/", json=VALID | {"quantity": 6000}).status_code == 409
    monkeypatch.setenv("PAPER_ENTRIES_ENABLED", "false")
    get_settings.cache_clear()
    assert client.post("/api/v1/positions/", json=VALID).status_code == 409


def test_history_dashboard_routes_and_unimplemented_scheduling(client):
    store = parquet_module.get_parquet_store()
    store.write_candles(pd.DataFrame({"timestamp": pd.date_range("2026-10-19T03:45:00Z", periods=2, freq="min"),
                                     "open": 100, "high": 100, "low": 100, "close": 100, "volume": 10,
                                     "oi": [float("nan"), 0]}), "TEST", "minute")
    response = client.get("/api/v1/history/TEST?interval=minute&limit=30")
    assert response.status_code == 200, response.text
    assert len(response.json()["candles"]) == 2
    assert [c["oi"] for c in response.json()["candles"]] == [None, 0]
    for url in ["/api/v1/ready", "/api/v1/liveness/feed", "/api/v1/pipeline/status", "/api/v1/watchlist/", "/api/v1/symbols/stored"]:
        assert client.get(url).status_code == 200
    assert client.post("/api/v1/pipeline/schedule").status_code == 501


def test_monitor_reads_canonical_parquet_candles(client):
    from datetime import UTC, datetime
    from trading_agent.risk.position_monitor import get_position_status
    now = datetime.now(UTC)
    parquet_module.get_parquet_store().write_candles(pd.DataFrame({
        "timestamp": [now], "open": [100], "high": [102], "low": [99], "close": [101], "volume": [10],
    }), "TEST", get_settings().storage.candle_interval)
    position = VALID | {"position_size": 10}
    status = asyncio.run(get_position_status(position, now=now, fetch_current_signals=False))
    assert status.current_price == 101
    assert status.pnl == 10


def test_position_sizing_uses_stored_prices_and_direction(client):
    from trading_agent.risk.portfolio_risk import calculate_position
    parquet_module.get_parquet_store().write_candles(pd.DataFrame({
        "timestamp": pd.date_range("2026-10-19T03:45:00Z", periods=40, freq="5min"),
        "open": 250, "high": 255, "low": 245, "close": 250, "volume": 100,
    }), "TEST", get_settings().storage.candle_interval)
    long = calculate_position("TEST", "BUY", 1000000)
    short = calculate_position("TEST", "SELL", 1000000)
    assert long.position_value / long.shares == 250
    assert short.stop_price > 250 > long.stop_price
    with pytest.raises(ValueError, match="Insufficient"):
        calculate_position("MISSING", "BUY", 1000000)


def test_force_cannot_bypass_running_pipeline(client, monkeypatch):
    from trading_agent.api.routers import pipeline
    monkeypatch.setitem(pipeline._pipeline_state, "status", "running")
    assert client.post("/api/v1/pipeline/run", json={"force": True}).status_code == 409


def test_authentication_protects_reads_and_writes(client, monkeypatch):
    public_status = client.get("/api/v1/auth/status")
    assert public_status.json() == {"required": False}
    assert public_status.headers["cache-control"] == "no-store"
    monkeypatch.setenv("API_AUTH_TOKEN", "test-secret")
    get_settings.cache_clear()
    with TestClient(create_app()) as protected:
        status = protected.get("/api/v1/auth/status")
        assert status.status_code == 200 and status.json() == {"required": True}
        assert "test-secret" not in status.text
        assert protected.get("/api/v1/auth/status", headers={"Authorization": "Bearer invalid"}).status_code == 200
        assert protected.get("/api/v1/health").status_code == 200
        assert protected.get("/api/v1/wallet/summary").status_code == 401
        assert protected.post("/api/v1/positions/", json=VALID).status_code == 401
        assert protected.get("/api/v1/wallet/summary", headers={"Authorization": "Bearer test-secret"}).status_code == 200


def test_alembic_upgrade_and_model_alignment(tmp_path):
    env = os.environ | {"DB_URL": f"sqlite:///{(tmp_path / 'migration.db').as_posix()}"}
    for command in ["upgrade head", "check"]:
        result = subprocess.run([sys.executable, "-m", "alembic", *command.split()], env=env,
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
