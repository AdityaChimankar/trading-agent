"""Offline bracket replay, evidence gates and chronological training regression tests."""
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from trading_agent.analysis import outcome_validation as validation
from trading_agent.analysis import research_engine as engine
from trading_agent.analysis import research_jobs as jobs
from trading_agent.api.routers import research
from trading_agent.core.config import PaperRiskSettings
from trading_agent.research.trade_outcomes import TradePolicy, replay, trade_outcome
from trading_agent.strategy.ml_features import create_target
from trading_agent.strategy.temporal_validation import folds, three_way
from trading_agent.strategy.train_ml_model_v2 import (
    EnhancedFeatureConfig, add_regime_features, create_cost_aware_target, create_triple_barrier_target,
)


def bars(n=90):
    return pd.DataFrame({"timestamp": pd.date_range("2026-10-20 04:00Z", periods=n, freq="min"),
                         "open": 100., "high": 101., "low": 99., "close": 100., "atr": 2.})


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_same_bar_stop_first_and_next_open(side):
    df = bars()
    df.loc[1, ["high", "low"]] = [110., 90.]
    trade = trade_outcome(df, 0, side, TradePolicy(cost_bps=0, slippage_bps=0))
    assert trade["entry"] == 100
    assert trade["exit_reason"] == "ambiguous_stop_first"
    assert trade["net_return"] == pytest.approx(-.03)


@pytest.mark.parametrize("side,gap", [("BUY", 90.), ("SELL", 110.)])
def test_gap_stop_fills_worse_open(side, gap):
    df = bars()
    df.loc[2, ["open", "high", "low", "close"]] = [gap, gap+1, gap-1, gap]
    trade = trade_outcome(df, 0, side, TradePolicy(cost_bps=0, slippage_bps=0))
    assert trade["exit_reason"] == "gap_stop"
    assert trade["net_return"] == pytest.approx(-.1)


def test_target_before_later_stop_and_roundtrip_costs():
    df = bars()
    df.loc[1, "high"] = 107
    df.loc[2, "low"] = 90
    trade = trade_outcome(df, 0, "BUY", TradePolicy(cost_bps=10, slippage_bps=2))
    assert trade["exit_reason"] == "target"
    assert trade["exit_index"] == 1
    assert trade["entry"] > 100
    assert trade["exit"] < trade["target"]
    assert trade["net_return"] == pytest.approx((trade["exit"]-trade["entry"])/trade["entry"]-.001)


def test_unknown_future_gap_and_invalid_ohlc_not_losses():
    df = bars(5)
    assert trade_outcome(df, 0, "BUY") is None
    df = bars()
    df.loc[1, "timestamp"] += pd.Timedelta(minutes=3)
    assert trade_outcome(df, 0, "BUY") is None
    df = bars()
    df.loc[1, "low"] = 102.
    assert trade_outcome(df, 0, "BUY") is None


def test_session_exit_and_no_late_entry():
    df = bars(30)
    df["timestamp"] = pd.date_range("2026-10-20 09:48Z", periods=30, freq="min")
    trade = trade_outcome(df, 0, "BUY")
    assert trade["exit_reason"] == "session_exit"
    assert trade["exit_timestamp"].startswith("2026-10-20T09:54")
    assert trade_outcome(df, 1, "BUY") is None


def test_replay_purges_partition_and_prevents_overlap():
    df = bars(140)
    trades = replay(df, np.ones(140), np.full(140, .7), 60, 130, TradePolicy(horizon=5), 60)
    assert trades
    assert all(t["exit_index"] < 130 and t["signal_index"] < 125 for t in trades)
    assert all(a["exit_index"] <= b["signal_index"] for a, b in zip(trades, trades[1:]))


def test_fifteen_minute_exit_does_not_read_past_session_cutoff():
    df = bars(10)
    df["timestamp"] = pd.date_range("2026-10-20 09:15Z", periods=10, freq="15min")
    trade = trade_outcome(df, 0, "BUY", interval_seconds=900)
    assert trade["exit_reason"] == "session_exit"
    assert trade["exit_timestamp"].startswith("2026-10-20T09:30")
    assert trade_outcome(df, 1, "BUY", interval_seconds=900) is None


def qualified_fixture(monkeypatch):
    frame = bars(1000).assign(rsi=20., adx=30., pattern_code=1)
    monkeypatch.setattr(validation, "prepare_research_frame", lambda raw: raw)
    called = []
    def replay_fixture(df, actions, scores, start, end, policy, seconds):
        called.append((start, end))
        return [{"side": "BUY", "score": .8 if i % 2 else .5, "win": i % 2,
                 "net_return": .03 if i % 2 else -.01, "session": f"day-{i//10}"} for i in range(200)]
    monkeypatch.setattr(validation, "replay", replay_fixture)
    now = datetime(2026, 10, 21, tzinfo=UTC)
    report = validation.validate_frame("TEST", "minute", frame, TradePolicy(), now)
    return report, now, called


def test_independent_windows_and_qualified_evidence(monkeypatch):
    report, now, windows = qualified_fixture(monkeypatch)
    assert windows == [(0, 600), (600, 800), (800, 1000)]
    assert report["sides"]["BUY"]["status"] == "qualified_research"
    assert report["sides"]["SELL"]["status"] == "insufficient_evidence"
    monkeypatch.setattr(validation, "read_evidence", lambda *a: report)
    row = {"symbol": "TEST", "interval": "minute", "action": "BUY",
           "timestamp": now.isoformat(), "rule": {"confidence": .8}}
    value = validation.estimate(row, "rule_only", 2, now)
    assert value["status"] == "qualified_research"
    assert 0 < value["win_probability"] < 1
    for mode in ("ml_only", "rule_ml_combined"):
        assert validation.estimate(row, mode, 2, now)["win_probability"] is None
    assert validation.estimate(row, "rule_only", 3, now)["win_probability"] is None
    assert validation.estimate(row, "rule_only", 2, now+timedelta(days=8))["win_probability"] is None
    row["timestamp"] = report["data_end"]
    assert validation.estimate(row, "rule_only", 2, now)["win_probability"] is None


def test_failed_evidence_and_missing_report_never_probability(monkeypatch):
    report, now, _ = qualified_fixture(monkeypatch)
    row = {"symbol": "TEST", "interval": "minute", "action": "BUY",
           "timestamp": now.isoformat(), "rule": {"confidence": .8}}
    monkeypatch.setattr(validation, "read_evidence", lambda *a: report)
    report["sides"]["BUY"]["status"] = "failed_validation"
    assert validation.estimate(row, "rule_only", 2, now)["win_probability"] is None
    monkeypatch.setattr(validation, "read_evidence", lambda *a: {})
    assert validation.estimate(row, "rule_only", 2, now)["win_probability"] is None


def test_old_data_cannot_be_refreshed_into_fresh_evidence(monkeypatch):
    report, now, _ = qualified_fixture(monkeypatch)
    assert datetime.fromisoformat(report["expires_at"]) <= datetime.fromisoformat(report["data_end"])+timedelta(days=7)


def test_insufficient_samples_and_zero_trades(monkeypatch):
    frame = bars(1000).assign(rsi=20., adx=30., pattern_code=1)
    monkeypatch.setattr(validation, "prepare_research_frame", lambda raw: raw)
    monkeypatch.setattr(validation, "replay", lambda *a: [])
    report = validation.validate_frame("TEST", "minute", frame, TradePolicy())
    assert report["status"] == "insufficient_evidence"
    assert report["holdout"]["win_rate"] is None
    with pytest.raises(ValueError, match="1,000"):
        validation.validate_frame("TEST", "minute", frame.iloc[:999], TradePolicy())


def test_snapshot_rechecks_candle_expiry_and_default_evidence_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    job = jobs.ResearchJobs()
    row = {"symbol": "TEST", "interval": "minute", "timestamp": "2026-01-01T04:00:00+00:00",
           "stale": False, "plan": {"blocked": False, "quantity": 10, "position_value": 1000, "risk_amount": 10}}
    job.state["rows"] = [row]
    snap = job.snapshot()
    assert snap["stale"] == 1 and snap["eligible"] == 0
    assert snap["rows"][0]["plan"]["quantity"] == 0
    assert row["plan"]["quantity"] == 10  # Snapshot changes must not mutate persisted state.
    candidate = {"symbol": "TEST", "action": "BUY", "score": 99, "stale": False,
                 "price": 100, "atr": 2, "room_to_target": None}
    sized = engine.size_opportunities([candidate], 100000, [], PaperRiskSettings(), require_validated=True)
    assert sized[0]["plan"]["blocked"]


def test_entry_session_and_expiry_boundary():
    row = {"timestamp": "2026-10-20T04:00:00+00:00", "interval": "minute"}
    engine.refresh_eligibility(row, datetime(2026, 10, 20, 4, 2, 14, tzinfo=UTC))
    assert not row["stale"] and row["session_open"]
    engine.refresh_eligibility(row, datetime(2026, 10, 20, 4, 2, 15, tzinfo=UTC))
    assert row["stale"]
    engine.refresh_eligibility(row, datetime(2026, 10, 20, 12, tzinfo=UTC))
    assert not row["session_open"]


def test_api_validation_bounds_and_busy_lock(monkeypatch):
    app = FastAPI()
    app.include_router(research.router)
    monkeypatch.setattr(validation, "validate_symbol", lambda **kw: {"symbol": kw["symbol"], "policy": asdict(TradePolicy())})
    with TestClient(app) as client:
        assert client.post("/outcome-validation", json={"symbol": "test"}).json()["symbol"] == "TEST"
        for body in ({"symbol": "../secret"}, {"symbol": "TEST", "horizon": 0},
                     {"symbol": "TEST", "interval": "day"}, {"symbol": "TEST", "cost_bps": -1}):
            assert client.post("/outcome-validation", json=body).status_code == 422
        research._validation_lock.acquire()
        try:
            assert client.post("/outcome-validation", json={"symbol": "TEST"}).status_code == 409
        finally:
            research._validation_lock.release()
    assert research.ScanRequest().require_validated


def test_future_labels_remain_unknown_and_barriers_follow_path():
    df = bars().rename(columns={"atr": "atr_14"})
    assert create_target(df, 12, "direction").tail(12).isna().all()
    assert create_cost_aware_target(df, 12).tail(12).isna().all()
    df.loc[1, "high"] = 108
    df.loc[3, "low"] = 90
    target = create_triple_barrier_target(df, 12)
    assert target.iloc[0] == 1
    assert target.tail(12).isna().all()


def test_multi_symbol_partitions_share_time_boundaries_and_purge_labels():
    stamps = pd.Series(pd.date_range("2026-01-01", periods=400, freq="min", tz="UTC"))
    stamps = pd.concat([stamps, stamps]).reset_index(drop=True)
    ends = stamps + pd.Timedelta(minutes=12)
    for train, tune, test in [three_way(stamps, ends), *folds(stamps, ends, 3)]:
        assert ends.iloc[train].max() < stamps.iloc[tune].min()
        assert ends.iloc[tune].max() < stamps.iloc[test].min()
        assert set(stamps.iloc[train]).isdisjoint(stamps.iloc[test])
        assert len(test) % 2 == 0


def test_regime_quantiles_are_causal():
    rng = np.random.default_rng(42)
    df = pd.DataFrame({"close": 100+np.cumsum(rng.normal(0, .1, 800)),
                       "volume": rng.integers(100, 2000, 800)})
    full = add_regime_features(df, EnhancedFeatureConfig())
    prefix = add_regime_features(df.iloc[:650], EnhancedFeatureConfig())
    columns = [c for c in full if c.endswith("_quantile")]
    pd.testing.assert_frame_equal(full[columns].iloc[:650], prefix[columns])


def test_trainer_never_uses_holdout_for_early_stopping(monkeypatch):
    from trading_agent.strategy import train_ml_model as training
    stamps = pd.Series(pd.date_range("2026-01-01", periods=1000, freq="min", tz="UTC"))
    X = pd.DataFrame({"row_id": np.arange(1000), "symbol": "TEST",
                      "__timestamp": stamps, "__label_end": stamps+pd.Timedelta(minutes=12)})
    y = pd.Series(np.arange(1000) % 2)
    captured = []
    monkeypatch.setattr(training.lgb, "Dataset", lambda data, label, **kw: (data, label))
    model = SimpleNamespace(best_iteration=1, predict=lambda data, **kw: np.full(len(data), .6),
                            feature_importance=lambda **kw: [1])
    def fit(params, data, valid_sets, **kw):
        captured.append(valid_sets)
        return model
    monkeypatch.setattr(training.lgb, "train", fit)
    trainer = training.MLTrainer(registry=SimpleNamespace(register=lambda **kw: kw["tags"]),
                                 parquet_store=SimpleNamespace())
    monkeypatch.setattr(trainer, "load_training_data", lambda *a: (X, y))
    result = trainer.train(["TEST"])
    assert all(part[0].row_id.max() < 800 for part in captured[0])
    assert list(captured[0][0][0].columns) == ["row_id"]
    assert result["metadata"]["split_policy"] == "global_timestamp_purged_train_tune_holdout"
    captured.clear()
    trainer.walk_forward_train(["TEST"], n_splits=3)
    for data, (_, _, holdout) in zip(captured, folds(stamps, X.__label_end, 3)):
        assert max(part[0].row_id.max() for part in data) < holdout.min()


def test_multi_symbol_loader_orders_by_time_and_preserves_label_end(monkeypatch):
    from trading_agent.strategy import train_ml_model as training
    from trading_agent.strategy.ml_features import FeatureConfig
    frame = bars(300)
    store = SimpleNamespace(read_candles=lambda *a: frame)
    def features(df, config):
        values = pd.DataFrame({"value": df.close}).iloc[20:-3]
        return values, pd.Series(np.arange(len(values)) % 2, index=values.index)
    monkeypatch.setattr(training, "prepare_features", features)
    trainer = training.MLTrainer(FeatureConfig(target_horizon=3, target_type="direction"),
                                 registry=SimpleNamespace(), parquet_store=store)
    X, y = trainer.load_training_data(["A", "B"])
    assert X.__timestamp.is_monotonic_increasing
    assert X.groupby("__timestamp").symbol.nunique().eq(2).all()
    assert (X.__label_end-X.__timestamp).eq(pd.Timedelta(minutes=3)).all()
    assert len(X) == len(y)


def test_enhanced_training_selects_threshold_on_tuning_not_holdout(monkeypatch):
    from trading_agent.strategy import train_ml_model_v2 as training
    frame = bars(1000).assign(row_id=np.arange(1000))
    y = pd.Series(np.arange(1000) % 2, dtype=float)
    y.iloc[-12:] = np.nan
    captured, threshold_samples = [], []
    monkeypatch.setattr(training.lgb, "Dataset", lambda data, label, **kw: (data, label))
    model = SimpleNamespace(best_iteration=1, predict=lambda data, **kw: np.full(len(data), .6),
                            feature_importance=lambda **kw: [1, 1])
    def fit(params, data, valid_sets, **kw):
        captured.append(valid_sets)
        return model
    def threshold(labels, predictions):
        threshold_samples.append(labels.index.to_numpy())
        return np.array([.5, 1.]), np.array([1., 0.]), np.array([.6])
    monkeypatch.setattr(training.lgb, "train", fit)
    monkeypatch.setattr(training, "precision_recall_curve", threshold)
    trainer = training.EnhancedMLTrainer(
        registry=SimpleNamespace(register=lambda **kw: kw["tags"]),
        parquet_store=SimpleNamespace(read_candles=lambda *a: frame),
    )
    monkeypatch.setattr(trainer, "prepare_enhanced_features", lambda df: df)
    monkeypatch.setattr(trainer, "create_targets", lambda *a: {"cost_aware": y})
    result = trainer._train_single_symbol("TEST", "minute", 60, "cost_aware", None)
    _, tune, holdout = three_way(frame.timestamp.iloc[:-12], frame.timestamp.shift(-12).iloc[:-12])
    np.testing.assert_array_equal(threshold_samples[0], tune)
    assert max(part[0].row_id.max() for part in captured[0]) < holdout.min()
    assert result["optimal_threshold"] == .6
    assert result["metadata"]["split_policy"] == "timestamp_purged_train_tune_holdout"
