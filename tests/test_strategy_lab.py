import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from trading_agent.research import strategy_lab as lab
from trading_agent.core.signals import SignalParams
from trading_agent.research.trade_outcomes import replay, TradePolicy


@pytest.fixture
def candles():
    stamps = pd.DatetimeIndex(np.concatenate([
        pd.date_range(day + " 03:45", periods=200, freq="min", tz="UTC").to_numpy()
        for day in ("2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-12")
    ]))
    rng = np.random.default_rng(2)
    close = 100 + rng.normal(0, .25, len(stamps)).cumsum()
    return pd.DataFrame({"timestamp": stamps, "open": close, "high": close+.5,
                         "low": close-.5, "close": close, "volume": rng.integers(50000, 100000, len(stamps))})


def test_profile_features_and_actions_are_causal(candles):
    full = lab.prepare_frame(candles)
    short = lab.prepare_frame(candles.iloc[:500])
    for profile, _, _ in lab.PROFILES:
        np.testing.assert_array_equal(lab.profile_actions(full, profile)[:500], lab.profile_actions(short, profile))


def test_comparison_same_window_fixed_policy_and_no_promotion(candles):
    result = lab.compare_profiles(candles, "TEST")
    assert result["status"] == "completed"
    assert result["scope"] == "exploratory_only"
    assert not result["promotion_eligible"]
    assert result["policy"]["entry_delay_bars"] == 1
    assert [p["id"] for p in result["profiles"]] == [p[0] for p in lab.PROFILES]
    assert result["windows"]["reference_end"] < result["windows"]["evaluation_start"]
    assert all(p["metrics"]["trades"] == sum(s["trades"] for s in p["sides"].values()) for p in result["profiles"])


def test_lab_uses_next_open_nonoverlapping_bracket_replay(candles, monkeypatch):
    monkeypatch.setattr(lab, "profile_actions", lambda frame, **kwargs: np.ones(len(frame), dtype=int))
    frame = lab.prepare_frame(candles)
    trades = replay(frame, np.ones(len(frame)), np.zeros(len(frame)), 0, len(frame), TradePolicy(), 60)
    assert trades
    assert all(a["exit_index"] <= b["signal_index"] for a, b in zip(trades, trades[1:]))
    assert trades[0]["entry"] == pytest.approx(frame.open.iloc[trades[0]["signal_index"]+1] * 1.0002)
    result = lab.StrategyLab().test_params(candles, SignalParams())
    assert result.total_return == pytest.approx(sum(t["net_return"] for t in trades) * 100)
    assert result.sharpe is None and result.gross_ev is None


def test_zero_trades_and_insufficient_data_are_not_fabricated(candles):
    metrics = lab.summarize([])
    assert metrics["win_rate_pct"] is None and metrics["profit_factor"] is None
    assert lab.compare_profiles(candles.iloc[:10], "TEST")["status"] == "insufficient_history"
    with pytest.raises(ValueError):
        lab.LabConfig(commission=20).policy()
    with pytest.raises(ValueError):
        lab.compare_profiles(candles, "TEST", "day")
    invalid = candles.copy()
    invalid.loc[1, "low"] = 10000
    with pytest.raises(ValueError, match="OHLCV"):
        lab.prepare_frame(invalid)


def test_api_bounds_and_lock(monkeypatch, candles):
    from trading_agent.api.routers import strategy_lab as route
    app = FastAPI()
    app.include_router(route.router)
    monkeypatch.setattr(route, "read_completed", lambda *args: candles.iloc[:10])
    with TestClient(app) as client:
        response = client.post("/compare", json={"symbol": "test", "interval": "minute"})
        assert response.status_code == 200
        assert response.json()["symbol"] == "TEST"
        assert client.post("/compare", json={"symbol": "../escape"}).status_code == 422
        assert client.post("/compare", json={"symbol": "TEST", "interval": "day"}).status_code == 422
        with route._lock:
            assert client.post("/compare", json={"symbol": "TEST"}).status_code == 409
