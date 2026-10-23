"""Risk controls, execution, analytics, volume and drift monitoring (Phase 3)."""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from trading_agent.api.main import create_app
from trading_agent.core.config import get_settings
from trading_agent.execution import costs
from trading_agent.storage import db as db_module, parquet_store as parquet_module

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "ledger.db"
    monkeypatch.setenv("DB_URL", f"sqlite:///{path.as_posix()}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ML_MODEL_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("PAPER_INITIAL_CAPITAL", "1000000")
    monkeypatch.setenv("API_AUTH_TOKEN", "")
    monkeypatch.setenv("EXECUTION_SLIPPAGE_BPS", "10")
    monkeypatch.setattr(db_module, "DB_PATH", str(path))
    store = parquet_module.ParquetStore(tmp_path / "parquet")
    monkeypatch.setattr(parquet_module, "_store", store)
    # No volume history in these tests unless a test provides one.
    monkeypatch.setattr("trading_agent.analysis.volume_insights.symbol_volume", lambda symbol, interval="minute": None)
    get_settings.cache_clear()
    db_module.reset_database()
    try:
        with TestClient(create_app()) as instance:
            instance.store = store
            yield instance
    finally:
        db_module.reset_database()
        get_settings.cache_clear()


ENTRY = {"symbol": "TEST", "side": "BUY", "quantity": 10, "reference_price": 100.0, "stop_loss": 95.0,
         "take_profit": 110.0}


def _candles(store, symbol, close):
    ts = pd.date_range(end=datetime.now(UTC).replace(second=0, microsecond=0), periods=5, freq="min")
    df = pd.DataFrame({"timestamp": ts, "open": close, "high": close + 1, "low": close - 1, "close": close,
                       "volume": 1000, "oi": 0})
    store.write_candles(df, symbol, get_settings().storage.candle_interval)


# --- costs -----------------------------------------------------------------------

def test_intraday_charges_match_the_schedule():
    buy = costs.order_charges("BUY", 1000, 100)    # turnover 1,00,000
    assert buy["brokerage"] == 20 and buy["stamp"] == 3 and buy["stt"] == 0
    assert buy["total"] == pytest.approx(30.22, abs=0.01)
    sell = costs.order_charges("SELL", 1000, 100)
    assert sell["stt"] == 25 and sell["stamp"] == 0
    assert sell["total"] == pytest.approx(52.22, abs=0.01)
    assert costs.order_charges("BUY", 100, 10)["brokerage"] == pytest.approx(0.3)  # 0.03% below the cap
    assert costs.round_trip_charges("BUY", 1000, 1000, 100) == pytest.approx(30.22 + 52.22, abs=0.01)
    assert costs.slipped_price("BUY", 100, 10) == 100.1 and costs.slipped_price("SELL", 100, 10) == 99.9


# --- controls --------------------------------------------------------------------

def test_kill_switch_blocks_entries_until_resumed(client):
    assert client.post("/api/v1/controls/halt", json={"reason": "broker outage"}).json()["halted"] is True
    blocked = client.post("/api/v1/execution/enter", json=ENTRY)
    assert blocked.status_code == 409 and "Kill switch" in blocked.json()["detail"]
    manual = client.post("/api/v1/positions/", json={"symbol": "TEST", "action": "BUY", "entry_price": 100,
                                                      "quantity": 1, "stop_loss": 95})
    assert manual.status_code == 409  # manual paper entries obey it too
    client.post("/api/v1/controls/resume")
    assert client.post("/api/v1/execution/enter", json=ENTRY).status_code == 200
    events = [e["event"] for e in client.get("/api/v1/controls/audit").json()]
    assert {"kill_switch_engaged", "kill_switch_released", "order_filled", "position_opened"} <= set(events)


def test_unreadable_kill_switch_fails_closed(tmp_path):
    from trading_agent.risk import controls

    (get_settings().data_dir / "controls").mkdir(parents=True, exist_ok=True)
    controls._halt_path().write_text("{not json", encoding="utf-8")
    assert controls.halt_state()["halted"] is True


@pytest.mark.parametrize("hhmm,weekday,expected", [
    ("09:00", 0, "Before market open"), ("10:30", 0, None), ("15:00", 0, "entry cutoff"),
    ("11:00", 5, "weekend"),
])
def test_session_gate(monkeypatch, hhmm, weekday, expected):
    from trading_agent.risk import controls

    monkeypatch.setenv("PAPER_ENFORCE_SESSION", "true")
    get_settings.cache_clear()
    monday = datetime(2026, 10, 19, tzinfo=IST)
    hours, minutes = map(int, hhmm.split(":"))
    now = (monday + timedelta(days=weekday)).replace(hour=hours, minute=minutes)
    reason = controls.entry_block_reason([], now)
    assert (reason is None) if expected is None else (expected in reason)
    assert controls.square_off_due(now.replace(hour=15, minute=15)) is (weekday < 5)


def test_daily_trade_limit(monkeypatch):
    from trading_agent.risk import controls

    monkeypatch.setenv("PAPER_MAX_TRADES_PER_DAY", "2")
    get_settings.cache_clear()
    now = datetime(2026, 10, 19, 11, tzinfo=IST)
    rows = [{"opened_at": now - timedelta(minutes=5)}, {"opened_at": now - timedelta(days=1)}]
    assert controls.entry_block_reason(rows, now) is None
    rows.append({"opened_at": now - timedelta(minutes=1)})
    assert "Daily trade limit" in controls.entry_block_reason(rows, now)


# --- execution -------------------------------------------------------------------

def test_paper_router_fills_with_slippage_and_charges(client):
    from trading_agent.risk.ledger_guard import read_ledger_state

    filled = client.post("/api/v1/execution/enter", json=ENTRY).json()
    assert filled["fill_price"] == 100.1 and filled["mode"] == "paper"
    position = client.get("/api/v1/positions/").json()[0]
    assert position["entry_price"] == 100.1
    read_ledger_state()  # value/risk rewritten consistently at the fill price
    exited = client.post(f"/api/v1/execution/exit/{filled['position_id']}?price=105").json()
    assert exited["exit_price"] == pytest.approx(104.895, abs=0.01)
    orders = client.get("/api/v1/execution/orders").json()
    assert [o["status"] for o in orders] == ["FILLED", "FILLED"]
    assert all(o["charges"] > 0 for o in orders)
    assert client.get("/api/v1/execution/reconcile").json()["ok"] is True


def test_rejected_entry_releases_the_reservation(client):
    from trading_agent.execution.router import BrokerRejected, OrderRouter

    class Rejecting:
        mode, slippage_bps = "paper", 0.0

        async def execute(self, order):
            raise BrokerRejected("RMS: insufficient margin")

    with pytest.raises(Exception, match="reservation released"):
        asyncio.run(OrderRouter(Rejecting()).enter(symbol="TEST", side="BUY", quantity=1, reference_price=100,
                                                   stop_loss=95, take_profit=None))
    assert client.get("/api/v1/positions/").json() == []
    assert client.get("/api/v1/execution/orders").json()[0]["status"] == "REJECTED"


def test_risk_rejection_never_reaches_the_broker(client):
    from trading_agent.execution.router import OrderRouter

    calls = []

    class Recording:
        mode, slippage_bps = "paper", 0.0

        async def execute(self, order):
            calls.append(order)

    with pytest.raises(Exception):  # 10% single-position limit on 10L = 1L; this is 6L
        asyncio.run(OrderRouter(Recording()).enter(symbol="TEST", side="BUY", quantity=6000, reference_price=100,
                                                   stop_loss=99, take_profit=None))
    assert calls == []


def test_participation_cap_blocks_oversized_orders(client, monkeypatch):
    monkeypatch.setattr("trading_agent.analysis.volume_insights.symbol_volume", lambda symbol, interval="minute": {
        "liquidity": "low", "max_position_value": 500.0, "max_quantity": 5, "participation_pct": 1.0, "reason": ""})
    response = client.post("/api/v1/execution/enter", json=ENTRY)
    assert response.status_code == 409 and "average daily traded value" in response.json()["detail"]


def test_live_mode_needs_explicit_confirmation(monkeypatch):
    from trading_agent.execution.router import LIVE_CONFIRMATION, live_enabled

    monkeypatch.setenv("EXECUTION_MODE", "live")
    get_settings.cache_clear()
    assert live_enabled() is False
    monkeypatch.setenv("EXECUTION_LIVE_CONFIRMATION", LIVE_CONFIRMATION)
    get_settings.cache_clear()
    assert live_enabled() is True


@pytest.mark.parametrize("final,expect_fill", [({"status": "COMPLETE", "average_price": 101.5}, True),
                                               ({"status": "REJECTED", "status_message": "Margin"}, False)])
def test_kite_broker_confirms_through_order_history(final, expect_fill):
    from trading_agent.execution.router import BrokerRejected, KiteBroker

    placed = {}
    kite = SimpleNamespace(place_order=lambda **kw: placed.update(kw) or "ORD1",
                           order_history=lambda oid: [{"status": "OPEN"}, final])
    broker = KiteBroker(kite=kite, poll_seconds=0.01)
    order = SimpleNamespace(symbol="INFY", side="BUY", quantity=3, product="MIS", client_order_id="a" * 32)
    if expect_fill:
        fill = asyncio.run(broker.execute(order))
        assert fill.price == 101.5 and fill.broker_order_id == "ORD1"
        assert placed["order_type"] == "MARKET" and placed["product"] == "MIS"
    else:
        with pytest.raises(BrokerRejected, match="Margin"):
            asyncio.run(broker.execute(order))


def test_reconcile_flags_broker_mismatch(client):
    from trading_agent.execution.reconcile import reconcile

    client.post("/api/v1/execution/enter", json=ENTRY)
    report = asyncio.run(reconcile(broker_positions=[
        {"tradingsymbol": "TEST", "quantity": 4, "product": "MIS", "exchange": "NSE"},
        {"tradingsymbol": "GHOST", "quantity": -2, "product": "MIS", "exchange": "NSE"}]))
    kinds = {(i["kind"], i["symbol"]) for i in report["issues"]}
    assert kinds == {("broker_quantity_mismatch", "TEST"), ("broker_quantity_mismatch", "GHOST")}


def test_square_off_closes_priced_positions_and_reports_the_rest(client):
    from trading_agent.risk.controls import square_off_all

    client.post("/api/v1/execution/enter", json=ENTRY)
    client.post("/api/v1/execution/enter", json=ENTRY | {"symbol": "NOPRICE"})
    _candles(client.store, "TEST", 103.0)
    results = {r["symbol"]: r["status"] for r in asyncio.run(square_off_all())}
    assert results == {"TEST": "closed", "NOPRICE": "failed"}
    assert [p["symbol"] for p in client.get("/api/v1/positions/").json()] == ["NOPRICE"]


# --- analytics ---------------------------------------------------------------------

def test_performance_report_nets_costs(client):
    for exit_price in (105, 98, 104):
        filled = client.post("/api/v1/execution/enter", json=ENTRY).json()
        client.post(f"/api/v1/execution/exit/{filled['position_id']}?price={exit_price}")
    report = client.get("/api/v1/analytics/performance").json()
    overall = report["overall"]
    assert overall["trades"] == 3
    assert overall["charges"] > 0 and overall["net_pnl"] < overall["gross_pnl"]
    assert overall["win_rate_pct"] == pytest.approx(66.7)
    assert report["by_strategy"][0]["strategy"] == "router"
    assert report["recent_trades"][0]["charges_modelled"] is False
    assert report["sample_warning"]


def test_summary_math():
    from trading_agent.analysis.performance import summarize

    rows = [{"net_pnl": 100, "gross_pnl": 110, "closed_at": "2026-10-19T10:00", "r_multiple": 1.0, "holding_minutes": 10},
            {"net_pnl": -50, "gross_pnl": -40, "closed_at": "2026-10-20T10:00", "r_multiple": -0.5, "holding_minutes": 20}]
    s = summarize(rows, 10_000)
    assert s["profit_factor"] == 2.0 and s["expectancy"] == 25.0 and s["avg_r_multiple"] == 0.25
    assert s["max_drawdown_pct"] == pytest.approx(50 / 10_100 * 100, abs=1e-3)


def _sessions(n_days=21, today_volume_mult=1.0, today_trend=0.0, value=50_000):
    rows = []
    start = datetime(2026, 9, 1, 3, 45, tzinfo=UTC)
    for d in range(n_days):
        day = start + timedelta(days=d)
        last_day = d == n_days - 1
        bars = 120 if last_day else 375
        for m in range(bars):
            price = 100 + (today_trend * m if last_day else 0)
            vol = value * (today_volume_mult if last_day else 1.0)
            rows.append({"timestamp": day + timedelta(minutes=m), "open": price, "high": price + 0.2,
                         "low": price - 0.2, "close": price, "volume": vol})
    return pd.DataFrame(rows)


def test_volume_profile_recommendations():
    from trading_agent.analysis.volume_insights import agreement, volume_profile

    surge = volume_profile(_sessions(today_volume_mult=2.0, today_trend=0.05), "UP")
    assert surge["rvol"] == pytest.approx(2.0, rel=0.01)
    assert surge["recommendation"] == "BUY_CONFIRMED" and surge["price_vs_vwap"] == "above"
    assert agreement("BUY_CONFIRMED", "BUY") == "confirms" and agreement("BUY_CONFIRMED", "SELL") == "contradicts"
    thin = volume_profile(_sessions(today_volume_mult=0.5), "THIN")
    assert thin["recommendation"] == "AVOID"
    illiquid = volume_profile(_sessions(value=10), "TINY")
    assert illiquid["liquidity"] == "illiquid" and illiquid["recommendation"] == "AVOID"
    # Cap = 1% of average daily traded value: 375 bars * 50,000 * 100 = 18.75 Cr -> 18.75 L.
    assert surge["max_position_value"] == pytest.approx(0.01 * 375 * 50_000 * 100, rel=1e-6)


# --- drift --------------------------------------------------------------------------

def _feature_frame(n, shift=0.0, seed=0):
    from trading_agent.strategy.research_features import FEATURES

    rng = np.random.default_rng(seed)
    data = {name: rng.normal(shift, 1, n) for name in FEATURES}
    data["pattern_code"] = rng.integers(0, 3, n).astype(float)
    return pd.DataFrame(data)


def test_drift_detection_and_fallback(client):
    from trading_agent.analysis.research_engine import model_for_research
    from trading_agent.strategy import model_monitoring as mm

    baseline = mm.baseline_from_frame(_feature_frame(5000), "synthetic")
    stable = mm.drift_report(live=_feature_frame(1000, seed=1), baseline=baseline)
    assert stable["status"] == "ok", stable["features"][:3]
    drifted = mm.drift_report(live=_feature_frame(1000, shift=1.5, seed=2), baseline=baseline)
    assert drifted["status"] == "drift" and len(drifted["drifted_features"]) >= 3
    assert "pattern_code" not in drifted["drifted_features"]

    applied = mm.evaluate_and_apply(drifted)
    assert applied["fallback"]["active"] is True
    model = model_for_research()
    assert isinstance(model, mm.DriftWithheldModel)
    assert model.predict(None, "X", "minute")["status"] == "drift_fallback"
    mm.clear_fallback()
    assert not isinstance(model_for_research(), mm.DriftWithheldModel)


def test_drift_needs_enough_live_data():
    from trading_agent.strategy import model_monitoring as mm

    baseline = mm.baseline_from_frame(_feature_frame(5000), "synthetic")
    assert mm.drift_report(live=_feature_frame(50), baseline=baseline)["status"] == "insufficient_live_data"
    assert mm.drift_report(live=_feature_frame(500), baseline=None)["status"] in {"no_baseline"}


def test_live_features_are_logged(client):
    from trading_agent.strategy import model_monitoring as mm
    from trading_agent.strategy.research_features import FEATURES

    row = pd.Series({name: 0.5 for name in FEATURES})
    mm.log_live_features("TEST", "minute", row)
    mm.log_live_features("BAD", "minute", row.replace(0.5, np.nan))
    logged = mm.read_live_features()
    assert logged["symbol"].tolist() == ["TEST"]


def test_controls_status_endpoint(client):
    status = client.get("/api/v1/controls/status").json()
    assert status["kill_switch"]["halted"] is False
    assert status["execution_mode"] == "paper"
    assert status["limits"]["square_off_time"] == "15:15"
    preview = client.get("/api/v1/execution/costs?price=1000&quantity=100").json()
    assert preview["breakeven_move_pct"] == pytest.approx(0.0825, abs=1e-3)
