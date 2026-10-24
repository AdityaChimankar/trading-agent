"""Adaptations from the external-repo review: re-entry rule, floor pivots, causal opening range."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from trading_agent.core.config import get_settings

IST = ZoneInfo("Asia/Kolkata")


# --- one entry per symbol per day -------------------------------------------------

def test_one_entry_per_symbol_per_day(monkeypatch):
    from trading_agent.risk import controls

    now = datetime(2026, 10, 19, 11, tzinfo=IST)
    rows = [{"symbol": "INFY", "opened_at": now - timedelta(minutes=30), "status": "closed"},
            {"symbol": "TCS", "opened_at": now - timedelta(days=1), "status": "closed"}]
    # Off by default: re-entry after a closed trade is allowed.
    assert controls.entry_block_reason(rows, now, symbol="INFY") is None

    monkeypatch.setenv("PAPER_ONE_ENTRY_PER_SYMBOL_PER_DAY", "true")
    get_settings.cache_clear()
    assert "already traded today" in controls.entry_block_reason(rows, now, symbol="infy")
    assert controls.entry_block_reason(rows, now, symbol="TCS") is None  # yesterday's trade doesn't count
    assert controls.entry_block_reason(rows, now) is None  # no symbol: rule not applicable
    get_settings.cache_clear()


def test_reentry_rule_applies_to_router_entries(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from trading_agent.api.main import create_app
    from trading_agent.storage import db as db_module

    path = tmp_path / "ledger.db"
    monkeypatch.setenv("DB_URL", f"sqlite:///{path.as_posix()}")
    monkeypatch.setenv("API_AUTH_TOKEN", "")
    monkeypatch.setenv("EXECUTION_SLIPPAGE_BPS", "0")
    monkeypatch.setenv("PAPER_ONE_ENTRY_PER_SYMBOL_PER_DAY", "true")
    monkeypatch.setattr(db_module, "DB_PATH", str(path))
    monkeypatch.setattr("trading_agent.analysis.volume_insights.symbol_volume", lambda symbol, interval="minute": None)
    get_settings.cache_clear()
    db_module.reset_database()
    entry = {"symbol": "TEST", "side": "BUY", "quantity": 10, "reference_price": 100.0, "stop_loss": 95.0}
    try:
        with TestClient(create_app()) as client:
            first = client.post("/api/v1/execution/enter", json=entry).json()
            client.post(f"/api/v1/execution/exit/{first['position_id']}?price=101")
            again = client.post("/api/v1/execution/enter", json=entry)
            assert again.status_code == 409 and "already traded today" in again.json()["detail"]
    finally:
        db_module.reset_database()
        get_settings.cache_clear()


# --- floor pivots -------------------------------------------------------------------

def _two_sessions():
    rows = []
    for day, (high, low, close) in {"2026-10-19": (110.0, 90.0, 100.0), "2026-10-20": (106.0, 101.0, 104.0)}.items():
        start = pd.Timestamp(f"{day} 09:15", tz=IST)
        for i in range(5):
            rows.append({"timestamp": start + pd.Timedelta(minutes=i), "open": close, "high": close, "low": close,
                         "close": close, "volume": 100})
        rows[-3].update(high=high)
        rows[-2].update(low=low)
    frame = pd.DataFrame(rows)
    frame["timestamp"] = frame["timestamp"].dt.tz_convert("UTC")
    return frame


def test_pivots_use_the_previous_session():
    from trading_agent.analysis.technical_summary import pivot_levels

    levels = pivot_levels(_two_sessions())
    pivot = (110 + 90 + 100) / 3
    assert levels["based_on"] == "2026-10-19" and levels["session"] == "2026-10-20"
    assert levels["pivot"] == pytest.approx(pivot, abs=0.01)
    assert levels["r1"] == pytest.approx(2 * pivot - 90, abs=0.01)
    assert levels["s1"] == pytest.approx(2 * pivot - 110, abs=0.01)
    assert levels["r2"] == pytest.approx(pivot + 20, abs=0.01)
    assert levels["s3"] == pytest.approx(90 - 2 * (110 - pivot), abs=0.01)
    # Price 104 sits between R1 (110) and the pivot (100).
    assert levels["nearest_resistance"] == pytest.approx(110, abs=0.01)
    assert levels["nearest_support"] == pytest.approx(pivot, abs=0.01)


def test_pivots_need_a_previous_session():
    from trading_agent.analysis.technical_summary import pivot_levels

    one_day = _two_sessions().iloc[5:]
    assert pivot_levels(one_day) is None


# --- causal opening range -----------------------------------------------------------

def test_opening_range_does_not_see_later_opening_bars():
    from trading_agent.strategy.session_features import add_intraday_patterns, add_session_features

    ts = pd.date_range("2026-10-19 09:15", periods=120, freq="min", tz=IST).tz_convert("UTC")
    rng = np.random.default_rng(3)
    close = 100 + rng.normal(0, 0.3, len(ts)).cumsum()
    base = pd.DataFrame({"timestamp": ts, "open": close, "high": close + 0.2, "low": close - 0.2,
                         "close": close, "volume": 1000.0})
    original = add_intraday_patterns(add_session_features(base))

    spiked = base.copy()
    spiked.loc[40, ["high", "close"]] = [150.0, 149.0]  # a spike at 09:55, inside the opening hour
    changed = add_intraday_patterns(add_session_features(spiked))

    cols = ["opening_range_high", "opening_range_low"]
    pd.testing.assert_frame_equal(original.loc[:39, cols], changed.loc[:39, cols])
    assert changed.loc[40:, "opening_range_high"].eq(150.0).all()  # carried forward after it happens
    assert original.loc[0, "opening_range_high"] == pytest.approx(base.loc[0, "high"])
