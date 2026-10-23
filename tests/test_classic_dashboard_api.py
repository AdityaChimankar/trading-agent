"""Classic React dashboard (/api/*) served from the current engine and ledger."""
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from trading_agent.api.main import create_app
from trading_agent.api.routers import legacy_ui
from trading_agent.core.config import get_settings
from trading_agent.storage import db as db_module, parquet_store as parquet_module


def _row(symbol, action, score, *, blocked=False, expires_in=600, rule=None, ml=None):
    entry = 100.0
    sign = 1 if action == "BUY" else -1
    return {
        "symbol": symbol, "action": action, "score": score, "interval": "minute",
        "timestamp": datetime.now(UTC).isoformat(), "price": entry, "atr": 2.0, "rsi": 40.0, "adx": 30.0,
        "patterns": ["hammer"], "pattern_bias": "bullish" if action == "BUY" else "bearish",
        "rule": {"action": rule or action, "confidence": 0.8},
        "ml": {"action": ml or action, "confidence": 0.7, "status": "ok"},
        "agreements": [f"Rule and ML agree on {action}"], "disagreements": [], "reasoning": "test",
        "room_to_target": 5.0, "stale": False,
        "expires_at": (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat(),
        "plan": {"entry_price": entry, "stop_loss": entry - sign * 3, "take_profit": entry + sign * 6,
                 "quantity": 0 if blocked else 10, "position_value": 0 if blocked else 1000,
                 "risk_amount": 0 if blocked else 30, "reward_risk": 2.0, "blocked": blocked,
                 "block_reason": "Portfolio position-count limit" if blocked else None,
                 "room_reward_risk": 5.0 / 3},
    }


@pytest.fixture
def scan_rows():
    return [_row("AAA", "BUY", 80), _row("BBB", "SELL", 70), _row("CCC", "BUY", 60, blocked=True),
            _row("OLD", "BUY", 50, expires_in=-60)]


@pytest.fixture
def client(tmp_path, monkeypatch, scan_rows):
    path = tmp_path / "ledger.db"
    monkeypatch.setenv("DB_URL", f"sqlite:///{path.as_posix()}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PAPER_INITIAL_CAPITAL", "1000000")
    monkeypatch.setenv("API_AUTH_TOKEN", "")
    monkeypatch.setenv("PAPER_ENTRIES_ENABLED", "true")
    monkeypatch.setenv("EXECUTION_SLIPPAGE_BPS", "0")  # exact fills keep P&L assertions readable
    monkeypatch.setattr(db_module, "DB_PATH", str(path))
    store = parquet_module.ParquetStore(tmp_path / "parquet")
    monkeypatch.setattr(parquet_module, "_store", store)
    monkeypatch.setattr(legacy_ui, "_scan", lambda: {"rows": scan_rows, "effective_capital": 100000.0})
    get_settings.cache_clear()
    db_module.reset_database()
    try:
        with TestClient(create_app()) as instance:
            instance.store = store
            yield instance
    finally:
        db_module.reset_database()
        get_settings.cache_clear()


def _write_candles(store, symbol, close, n=120):
    ts = pd.date_range(end=datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=2),
                       periods=n, freq="min")
    closes = np.full(n, float(close))
    df = pd.DataFrame({"timestamp": ts, "open": closes, "high": closes + 1, "low": closes - 1,
                       "close": closes, "volume": 1000, "oi": 0})
    for interval in {"minute", get_settings().storage.candle_interval}:
        store.write_candles(df, symbol, interval)


def test_rankings_and_opportunities_come_from_the_scan(client):
    ranked = client.get("/api/rankings").json()
    assert [e["symbol"] for e in ranked["scored"]] == ["AAA", "BBB", "CCC", "OLD"]
    assert [e["symbol"] for e in ranked["bullish"]] == ["AAA", "CCC", "OLD"]
    assert [e["blocked"] for e in ranked["bullish"]] == [False, True, True]  # OLD expired
    assert ranked["bearish"][0]["symbol"] == "BBB"

    opps = client.get("/api/opportunities").json()
    by_symbol = {o["symbol"]: o for o in opps["bullish"] + opps["bearish"]}
    assert by_symbol["AAA"]["blocked"] is False and by_symbol["AAA"]["suggested_size"] == 10
    assert by_symbol["AAA"]["technical_score"] == 80.0
    assert by_symbol["CCC"]["block_reason"] == "Portfolio position-count limit"
    # An expired plan is never offered as actionable.
    assert by_symbol["OLD"]["blocked"] is True
    assert opps["available_capital"] == 100000.0


def test_open_from_scan_plan_then_close_at_latest_candle(client):
    created = client.post("/api/positions", json={"symbol": "AAA", "action": "BUY", "capital": 100000})
    assert created.status_code == 201, created.text
    assert created.json()["shares"] == 10

    # Held symbols leave Top Opportunity and are listed as excluded.
    opps = client.get("/api/opportunities").json()
    assert "AAA" in opps["excluded"] and all(o["symbol"] != "AAA" for o in opps["bullish"])

    _write_candles(client.store, "AAA", 105)
    positions = client.get("/api/positions").json()
    assert positions[0]["position"]["symbol"] == "AAA" and positions[0]["pnl"] == pytest.approx(50)
    wallet = client.get("/api/wallet").json()
    assert wallet["today"]["open_positions_count"] == 1
    assert wallet["open_positions"][0]["stop_scenario_pnl"] == pytest.approx(-30)

    closed = client.post(f"/api/positions/{positions[0]['position']['id']}/close")
    assert closed.status_code == 200, closed.text
    assert closed.json()["exit_price"] == 105 and closed.json()["realized_pnl_rupees"] == 50

    history = client.get("/api/transactions").json()
    assert history["summary"]["trade_count"] == 1 and history["summary"]["wins"] == 1
    assert history["summary"]["realized_pnl"] == 50
    assert client.get("/api/wallet").json()["fixed"]["book_balance"] == 1000050


@pytest.mark.parametrize("payload", [{"symbol": "CCC", "action": "BUY"},   # risk-blocked
                                     {"symbol": "OLD", "action": "BUY"},   # expired
                                     {"symbol": "AAA", "action": "SELL"},  # wrong side
                                     {"symbol": "ZZZ", "action": "BUY"}])  # not scanned
def test_unactionable_plans_are_refused(client, payload):
    response = client.post("/api/positions", json=payload | {"capital": 100000})
    assert response.status_code == 400
    assert client.get("/api/positions").json() == []


def test_contradiction_flags_open_position_against_new_bias(client, scan_rows):
    assert client.post("/api/positions", json={"symbol": "AAA", "action": "BUY", "capital": 1}).status_code == 201
    _write_candles(client.store, "AAA", 98)
    scan_rows[0] = _row("AAA", "SELL", 75)
    ranked = client.get("/api/rankings").json()
    flagged = {e["symbol"]: e["contradiction"] for e in ranked["contradictions"]}
    assert "loss" in flagged["AAA"] and "bearish" in flagged["AAA"]


def test_cash_movements_are_refused_not_faked(client):
    assert client.post("/api/wallet/deposit").status_code == 409
    assert client.post("/api/wallet/withdraw").status_code == 409


def test_pipeline_reports_ml_agreement_with_rules(client, scan_rows):
    scan_rows.append(_row("DDD", "BUY", 40, rule="BUY", ml="SELL"))
    summary = client.get("/api/pipeline").json()["summary"]
    assert summary["ml_compared"] == 5
    assert summary["ml_agreement_pct"] == 80.0
    assert summary["sized"] == 3  # AAA, BBB, DDD; CCC blocked, OLD expired


def test_chart_and_patterns_use_research_frame(client):
    assert client.get("/api/symbols/AAA/chart").json()["insufficient_data"] is True
    _write_candles(client.store, "AAA", 100, n=200)
    chart = client.get("/api/symbols/AAA/chart?limit=50").json()
    assert chart["insufficient_data"] is False
    assert len(chart["candles"]["close"]) == 50
    assert set(chart["patterns"]) == {"doji", "hammer", "bullish_engulfing", "bearish_engulfing"}
    assert client.get("/api/symbols/AAA/patterns").json()["bias"] in {"neutral", "bullish", "bearish"}


def test_technicals_include_vote_and_pivots(client):
    assert client.get("/api/symbols/AAA/technicals").json()["insufficient_data"] is True
    # 2,000 minutes reaches back into an earlier IST session, so pivots exist.
    _write_candles(client.store, "AAA", 100, n=2000)
    body = client.get("/api/symbols/AAA/technicals").json()
    assert body["total"] == body["bullish_count"] + body["bearish_count"] + body["neutral_count"] > 0
    assert body["pivots"]["pivot"] == pytest.approx((101 + 99 + 100) / 3, abs=0.01)
    assert client.get("/api/symbols/AAA/technicals?interval=week").status_code == 422


def test_invalid_symbol_rejected(client):
    assert client.get("/api/symbols/..%2Fetc/news").status_code in {404, 422}
    assert client.get("/api/symbols/bad$sym/sizing").status_code == 422


def test_classic_routes_require_auth_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_URL", f"sqlite:///{(tmp_path / 'a.db').as_posix()}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("API_AUTH_TOKEN", "secret")
    get_settings.cache_clear()
    db_module.reset_database()
    try:
        client = TestClient(create_app())
        assert client.get("/api/wallet").status_code == 401
        assert client.get("/api/watchlist", headers={"Authorization": "Bearer secret"}).status_code == 200
    finally:
        db_module.reset_database()
        get_settings.cache_clear()
