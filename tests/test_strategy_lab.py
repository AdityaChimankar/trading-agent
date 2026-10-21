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


def test_evaluation_prices_cannot_change_reference_results(candles):
    original = lab.compare_profiles(candles, "TEST")
    changed = candles.copy()
    split = int(len(changed) * .6)
    changed.loc[split:, ["open", "high", "low", "close"]] *= 1.5
    altered = lab.compare_profiles(changed, "TEST")
    for before, after in zip(original["profiles"] + original["baselines"],
                             altered["profiles"] + altered["baselines"], strict=True):
        assert before["reference"] == after["reference"]


def test_partitions_own_their_outcomes_and_baselines_share_policy(monkeypatch, candles):
    calls = []

    def checked_replay(frame, actions, scores, start, end, policy, seconds):
        trades = replay(frame, actions, scores, start, end, policy, seconds)
        assert all(start <= trade["signal_index"] < trade["exit_index"] < end for trade in trades)
        calls.append((start, end, policy, seconds))
        return trades

    monkeypatch.setattr(lab, "replay", checked_replay)
    policy = TradePolicy(horizon=12, cost_bps=20, slippage_bps=5)
    result = lab.compare_profiles(candles, "TEST", policy=policy)
    assert [baseline["id"] for baseline in result["baselines"]] == ["always_long", "always_short"]
    assert len(calls) == 2 * (len(lab.PROFILES) + 2)
    assert set(calls) == {(0, 720, policy, 60), (720, 1200, policy, 60)}


@pytest.mark.parametrize("kind", ["duplicate", "unsorted", "nan", "negative_volume", "bad_timestamp"])
def test_rejects_invalid_candle_history(candles, kind):
    invalid = candles.iloc[:100].copy()
    if kind == "duplicate":
        invalid.loc[1, "timestamp"] = invalid.timestamp.iloc[0]
    elif kind == "unsorted":
        invalid = invalid.iloc[::-1]
    elif kind == "nan":
        invalid.loc[1, "close"] = np.nan
    elif kind == "negative_volume":
        invalid.loc[1, "volume"] = -1
    else:
        invalid.loc[1, "timestamp"] = pd.NaT
    with pytest.raises(ValueError, match="OHLCV"):
        lab.prepare_frame(invalid)


def test_empty_and_all_winning_metrics_are_finite_json():
    import json

    assert lab.summarize([])["total_net_return_pct"] is None
    metrics = lab.summarize([{"session": "2026-10-05", "net_return": -.02},
                             {"session": "2026-10-05", "net_return": .01}])
    assert metrics["max_drawdown_pct_points"] == pytest.approx(2)
    assert metrics["total_net_return_pct"] == pytest.approx(-1)
    assert metrics["profit_factor"] == pytest.approx(.5)
    winning = lab.summarize([{"session": "2026-10-05", "net_return": .01}])
    assert winning["profit_factor"] is None
    json.dumps([metrics, winning, lab.summarize([])], allow_nan=False)


def test_completed_read_is_bounded_and_excludes_live_or_future_candles(monkeypatch, candles):
    now = pd.Timestamp.now(tz="UTC")
    raw = candles.iloc[:3].copy()
    raw["timestamp"] = [now - pd.Timedelta(minutes=2), now - pd.Timedelta(seconds=30),
                        now + pd.Timedelta(seconds=30)]
    calls = []

    class Store:
        def read_latest(self, symbol, interval, n):
            calls.append((symbol, interval, n))
            return raw

    monkeypatch.setattr(lab, "get_parquet_store", Store)
    completed = lab.read_completed("TEST", "minute", 250)
    assert len(completed) == 1
    assert completed.timestamp.iloc[0] == raw.timestamp.iloc[0]
    assert calls == [("TEST", "minute", 251)]
    with pytest.raises(ValueError):
        lab.read_completed("TEST", "minute", 10001)
    assert len(calls) == 1


def test_api_releases_lock_after_error_and_validates_response(monkeypatch, candles):
    from trading_agent.api.routers import strategy_lab as route

    app = FastAPI()
    app.include_router(route.router)
    invalid = candles.copy()
    invalid.loc[1, "low"] = 10000
    monkeypatch.setattr(route, "read_completed", lambda *args: invalid)
    with TestClient(app) as client:
        assert client.post("/compare", json={"symbol": "TEST"}).status_code == 422
        assert not route._lock.locked()
        for fields in ({"max_bars": 10001}, {"horizon": 0}, {"cost_bps": "nan"},
                       {"slippage_bps": -1}, {"typo": True}):
            assert client.post("/compare", json={"symbol": "TEST", **fields}).status_code == 422
        monkeypatch.setattr(route, "read_completed", lambda *args: candles)
        result = client.post("/compare", json={"symbol": "test"})
        assert result.status_code == 200
        assert result.json()["status"] == "completed"
        assert result.json()["promotion_eligible"] is False


def test_legacy_search_bounds_and_zero_trades(monkeypatch, candles):
    monkeypatch.setattr(lab, "profile_actions", lambda frame, **kwargs: np.zeros(len(frame)))
    instance = lab.StrategyLab()
    result = instance.test_params(candles, SignalParams())
    assert result.total_trades == 0 and result.net_ev is None
    with pytest.raises(ValueError, match="trials"):
        instance.grid_search(candles, {"adx_threshold": list(range(257))})
    with pytest.raises(ValueError, match="trials"):
        instance.random_search(candles, {}, n_iter=0)


def test_cli_handles_unavailable_metrics_and_insufficient_history(monkeypatch, candles):
    from typer.testing import CliRunner
    from trading_agent.optimize.cli import app

    monkeypatch.setattr(lab, "read_completed", lambda *args: candles)
    monkeypatch.setattr(lab, "profile_actions", lambda frame, **kwargs: np.zeros(len(frame)))
    result = CliRunner().invoke(app, ["strategy-lab", "TEST", "--interval", "minute"])
    assert result.exit_code == 0, result.output
    assert "Mean net return: n/a" in result.output
    assert "Exploratory" in result.output
    monkeypatch.setattr(lab, "read_completed", lambda *args: candles.iloc[:10])
    result = CliRunner().invoke(app, ["strategy-lab", "TEST"])
    assert result.exit_code == 1 and "Insufficient history" in result.output
