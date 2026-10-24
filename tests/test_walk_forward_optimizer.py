"""Causal window, pricing and HTTP contracts; no downloads, training or orders."""
from dataclasses import asdict
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from trading_agent.core.signals import DEFAULT_PARAMS, FORWARD_WINDOW, evaluate_signals
from trading_agent.research import walk_forward_optimizer as wf


@pytest.fixture
def prepared():
    n = 1200
    return {"symbol": "TEST", "length": n, "rsis": np.full(n, 40.),
            "adxs": np.full(n, 30.), "opens": np.full(n, 100.),
            "closes": np.linspace(101, 120, n), "timestamps": np.arange(n)}


def test_default_signal_parameters_match_live_rule_evaluation():
    rsi = np.array([20., 40., 60., 80., np.nan])
    adx = np.full(5, 30.)
    patterns = np.array([1, 1, 2, 2, 1], dtype=np.int8)
    np.testing.assert_array_equal(wf.vectorized_evaluate(rsi, adx, patterns, None),
                                  evaluate_signals(rsi, adx, patterns))
    assert set(wf.PARAM_GRID) == {"pattern_confirm_buy_rsi", "pattern_confirm_sell_rsi", "adx_threshold"}
    assert len(wf._grid_combinations()) == 27


def test_fold_purges_outcomes_and_uses_next_open_consistent_costs(prepared):
    codes = np.ones(prepared["length"], dtype=np.int8)
    trades = wf.vectorized_trades(prepared, codes, None, (0, 400), cost_bps=10)
    assert trades
    for trade in trades:
        i = trade["signal_index"]
        assert trade["entry_index"] == i + 1
        assert trade["exit_index"] == i + FORWARD_WINDOW < 400
        assert trade["entry"] == prepared["opens"][i + 1]
        assert trade["return_pct"] == pytest.approx((trade["exit"] - 100) / 100 - .001)
    assert all(b["entry_index"] > a["exit_index"] for a, b in zip(trades, trades[1:]))
    score, count = wf.vectorized_score(prepared, codes, asdict(DEFAULT_PARAMS), (0, 400), 10)
    assert count == len(trades)
    assert score == pytest.approx(sum(trade["return_pct"] for trade in trades))


def test_future_prices_cannot_change_training_selection(prepared):
    codes = np.ones(prepared["length"], dtype=np.int8)
    before = wf.optimize_fold(prepared, codes, (0, 400))
    score_before = wf.vectorized_score(prepared, codes, before, (0, 400))
    prepared["opens"][400:] = 10000
    prepared["closes"][400:] = .01
    assert wf.optimize_fold(prepared, codes, (0, 400)) == before
    assert wf.vectorized_score(prepared, codes, before, (0, 400)) == score_before


def test_oos_windows_disjoint_and_baseline_comparable(prepared):
    result = wf.evaluate_walk_forward(prepared, np.ones(1200, dtype=np.int8), 400, 200)
    assert result["status"] == "completed"
    assert result["evaluated_folds"] == 4
    assert result["optimized"] == result["default"]
    for i, fold in enumerate(result["folds"]):
        assert fold["train_end_exclusive"] == fold["test_start"] == 400 + 200 * i
        assert fold["test_end_exclusive"] == 600 + 200 * i
        if i:
            assert result["folds"][i - 1]["test_end_exclusive"] == fold["test_start"]


def test_no_training_trades_has_no_claimed_optimized_performance(prepared):
    result = wf.evaluate_walk_forward(prepared, np.zeros(1200, dtype=np.int8), 400, 200)
    assert result["status"] == "insufficient_training_trades"
    assert result["evaluated_folds"] == 0
    assert result["optimized"]["trades"] == 0
    assert all(fold["params"] is None for fold in result["folds"])


def test_drawdown_includes_initial_zero_and_returns_are_additive():
    result = wf._summarize_quiet([{"return_pct": -.1}, {"return_pct": .1}])
    assert result["max_drawdown_pct"] == -10
    assert result["total_return_pct"] == 0


def test_stored_frame_and_selected_interval(monkeypatch, sample_ohlcv_data):
    from trading_agent.storage import parquet_store
    seen = []
    def read_latest(symbol, interval, n):
        seen.append((symbol, interval, n))
        return sample_ohlcv_data
    monkeypatch.setattr(parquet_store, "get_parquet_store", lambda: SimpleNamespace(read_latest=read_latest))
    result = wf.run_walk_forward("TEST", False, interval="5minute", train_candles=250, test_candles=100)
    assert seen == [("TEST", "5minute", 20000)]
    assert result["candles"] == 500
    assert len(result["folds"]) == 2
    missing = wf.run_walk_forward("TEST", False)
    assert missing["status"] == "insufficient_history"
    assert missing["required_candles"] == 4500


@pytest.fixture
def client():
    from trading_agent.api.routers import research
    app = FastAPI()
    app.include_router(research.router, prefix="/research")
    with TestClient(app) as client:
        yield client


def test_api_normalization_and_no_implicit_training(client, monkeypatch):
    calls = []
    def run(**kwargs):
        calls.append(kwargs)
        return {"status": "insufficient_history"}
    monkeypatch.setattr(wf, "run_walk_forward", run)
    assert client.post("/research/walk-forward", json={"symbol": " test "}).status_code == 200
    assert calls == [{"symbol": "TEST", "verbose": False, "interval": "minute",
                      "train_candles": 3750, "test_candles": 750, "cost_bps": 5}]


@pytest.mark.parametrize("changes", [
    {"symbol": "../private"}, {"interval": "invalid"}, {"train_candles": 0},
    {"train_candles": 10001}, {"test_candles": 99}, {"cost_bps": -1}, {"cost_bps": 101},
])
def test_api_rejects_invalid_or_unbounded_request(client, changes):
    assert client.post("/research/walk-forward", json={"symbol": "TEST", **changes}).status_code == 422


def test_api_busy_guard_and_release_after_error(client, monkeypatch):
    from trading_agent.api.routers import research
    research._optimizer_lock.acquire()
    try:
        assert client.post("/research/walk-forward", json={"symbol": "TEST"}).status_code == 409
    finally:
        research._optimizer_lock.release()
    def fail(**kwargs):
        raise ValueError("Invalid data")
    monkeypatch.setattr(wf, "run_walk_forward", fail)
    for _ in range(2):
        assert client.post("/research/walk-forward", json={"symbol": "TEST"}).status_code == 422
