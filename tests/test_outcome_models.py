"""Candidate lifecycle tests without changing installed models or the real paper book."""
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from threadpoolctl import threadpool_limits

from trading_agent.research.trade_outcomes import TradePolicy, outcome_arrays, trade_outcome
from trading_agent.strategy import outcome_contract as contract
from trading_agent.strategy import outcome_model as adapter
from trading_agent.strategy import outcome_paper as paper
from trading_agent.strategy import train_outcome_models as trainer


def raw_frame(sessions=50, bars=140):
    rng = np.random.default_rng(13)
    stamps = pd.DatetimeIndex([stamp for day in pd.bdate_range("2026-01-01", periods=sessions)
                              for stamp in pd.date_range(day.tz_localize("UTC")+pd.Timedelta(hours=4),
                                                         periods=bars, freq="min")])
    close = 100+np.cumsum(rng.normal(0, .3, len(stamps)))
    return pd.DataFrame({"timestamp": stamps, "open": close+rng.normal(0, .1, len(stamps)),
                         "high": close+.8, "low": close-.8, "close": close, "volume": 100000})


@pytest.mark.parametrize("side", contract.SIDES)
@pytest.mark.parametrize("delay", [1, 2])
def test_vector_labels_match_scalar_execution_oracle(side, delay):
    df = contract.frame_features(raw_frame(sessions=3))
    df.loc[80, "low"] = np.nan
    df.loc[160, "timestamp"] += pd.Timedelta(minutes=2)
    policy = TradePolicy(entry_delay_bars=delay)
    net, exits = outcome_arrays(df, side, policy)
    for i in range(len(df)):
        outcome = trade_outcome(df, i, side, policy)
        if outcome is None:
            assert np.isnan(net[i]) and exits[i] == -1
        else:
            assert net[i] == pytest.approx(outcome["net_return"])
            assert exits[i] == outcome["exit_index"]


def winning_trades(side="BUY", sessions=30, count=10, win_count=8):
    return [{"side": side, "session": f"session-{day}", "net_return": .02 if i < win_count else -.01}
            for day in range(sessions) for i in range(count)]


def test_gate_requires_more_than_sixty_and_positive_net_expectancy():
    assert contract.metrics(winning_trades())["passed"]
    assert not contract.metrics(winning_trades(win_count=6))["passed"]
    # High win rate with catastrophic losses is not a profitable strategy.
    losses = winning_trades()
    for trade in losses:
        if trade["net_return"] < 0:
            trade["net_return"] = -.5
    assert not contract.metrics(losses)["passed"]
    assert not contract.metrics(winning_trades(sessions=5), forward=True)["passed"]


def test_joint_direction_conflicts_abstain():
    np.testing.assert_array_equal(contract.decisions(
        np.array([.8, .2, .8, .2]), np.array([.2, .8, .8, .2]), {"BUY": .6, "SELL": .6}),
        [1, 2, 0, 0])


def test_causal_feature_contract_and_full_horizon_purge():
    raw = raw_frame()
    full = contract.frame_features(raw)
    cut = trainer.partitions({"A": full, "B": full})[0]
    prefix = contract.frame_features(raw.iloc[:1000])
    pd.testing.assert_frame_equal(prefix[contract.FEATURE_COLUMNS], full[contract.FEATURE_COLUMNS].iloc[:1000])
    mask = trainer.partition_mask(full, None, cut)
    label_ends = full.timestamp.shift(-(contract.POLICY.horizon+contract.POLICY.entry_delay_bars-1))+pd.Timedelta(minutes=1)
    assert (label_ends[mask] < cut).all()


def test_training_artifact_roundtrip_and_no_reuse_of_holdout(tmp_path, monkeypatch):
    monkeypatch.setattr(trainer, "get_settings", lambda: SimpleNamespace(ml=SimpleNamespace(model_dir=tmp_path)))
    raw = raw_frame()
    monkeypatch.setattr(trainer, "get_parquet_store", lambda: SimpleNamespace(read_latest=lambda *a, **kw: raw))
    report = trainer.train_candidate(["TEST"])
    bundle, stored = trainer.read_candidate(report["candidate_id"])
    assert bundle["features"] == contract.FEATURE_COLUMNS
    assert set(bundle["sides"]) == {"BUY", "SELL"}
    assert stored["status"] in {"historical_gate_failed", "awaiting_forward_paper"}
    assert not (tmp_path/"outcome-active.json").exists()
    with pytest.raises(ValueError, match="already reserved"):
        trainer.train_candidate(["DIFFERENT_SYMBOL"])
    artifact = trainer.candidate_path(report["candidate_id"])/"model.joblib"
    artifact.write_bytes(artifact.read_bytes()+b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        trainer.read_candidate(report["candidate_id"])


def test_test_prices_cannot_change_fitted_models_or_selected_thresholds():
    raw = raw_frame()
    original = contract.frame_features(raw)
    cut = trainer.partitions({"TEST": original})[2]
    changed = raw.copy()
    mask = changed.timestamp >= cut
    changed.loc[mask, ["open", "high", "low", "close"]] *= 1.2
    with threadpool_limits(limits=2):
        one, _ = trainer.fit_data({"TEST": original}, "a"*32, datetime.now(UTC).isoformat())
        two, _ = trainer.fit_data({"TEST": contract.frame_features(changed)}, "b"*32, datetime.now(UTC).isoformat())
        assert one["thresholds"] == two["thresholds"]
        features = original.loc[contract.eligible_features(original)].head(100)
        for side in contract.SIDES:
            np.testing.assert_allclose(contract.probabilities(one["sides"][side], features),
                                       contract.probabilities(two["sides"][side], features))


class ConstantClassifier:
    classes_ = np.array([0, 1])

    def __init__(self, p):
        self.p = p

    def predict_proba(self, x):
        return np.tile([1-self.p, self.p], (len(x), 1))


class SigmoidCalibrator:
    def predict_proba(self, x):
        p = 1/(1+np.exp(-np.asarray(x).ravel()))
        return np.column_stack([1-p, p])


def fake_bundle():
    return {"candidate_id": "a"*32, "created_at": "2025-01-01T00:00:00+00:00",
            "test_end": "2025-01-01T00:00:00+00:00", "symbols": ["TEST"],
            "thresholds": {"BUY": .6, "SELL": .6}, "policy": {},
            "contract_hash": contract.contract_hash(),
            "sides": {side: {"model": ConstantClassifier(.8 if side == "BUY" else .2),
                             "calibrator": SigmoidCalibrator(), "mean_win": .02, "mean_loss": -.01}
                      for side in contract.SIDES},
            "test_trades": winning_trades("BUY")+winning_trades("SELL")}


def test_paper_records_before_entry_and_settles_only_future_bars(tmp_path, monkeypatch):
    bundle, report = fake_bundle(), {"artifact_sha256": "digest"}
    df = contract.frame_features(raw_frame(sessions=1, bars=130))
    stamp = df.timestamp.iloc[-1].to_pydatetime()
    conn = paper.connection(tmp_path)
    try:
        assert not paper.record_observation(conn, bundle, report, "TEST", df, stamp+timedelta(minutes=2))
        assert paper.record_observation(conn, bundle, report, "TEST", df, stamp+timedelta(seconds=65))
        assert not paper.record_observation(conn, bundle, report, "TEST", df, stamp+timedelta(seconds=70))
        assert paper.paper_report(conn, bundle, report)["pending"] == 1
        future = pd.DataFrame({"timestamp": pd.date_range(stamp+timedelta(minutes=1), periods=4, freq="min"),
                              "open": df.close.iloc[-1], "close": df.close.iloc[-1],
                              "high": df.close.iloc[-1]+20, "low": df.close.iloc[-1]-.1, "volume": 100000})
        full = contract.frame_features(pd.concat([df[future.columns], future], ignore_index=True))
        monkeypatch.setattr(paper, "latest", lambda *a: full)
        paper.settle(conn, bundle, report, stamp+timedelta(minutes=6))
        evidence = paper.paper_report(conn, bundle, report)
        assert evidence["settled"] == 1 and evidence["pending"] == 0
        assert not evidence["passed"]  # One winner is not forward confirmation.
    finally:
        conn.close()


def test_missing_future_outcomes_block_promotion_instead_of_disappearing(tmp_path, monkeypatch):
    bundle, report = fake_bundle(), {"artifact_sha256": "digest"}
    df = contract.frame_features(raw_frame(sessions=1, bars=130))
    stamp = df.timestamp.iloc[-1].to_pydatetime()
    conn = paper.connection(tmp_path)
    try:
        paper.record_observation(conn, bundle, report, "TEST", df, stamp+timedelta(seconds=65))
        monkeypatch.setattr(paper, "latest", lambda *a: df)
        paper.settle(conn, bundle, report, stamp+timedelta(minutes=30))
        evidence = paper.paper_report(conn, bundle, report)
        assert evidence["invalid"] == 1 and not evidence["passed"]
    finally:
        conn.close()


def test_promotion_requires_actual_forward_evidence_and_preserves_legacy(tmp_path, monkeypatch):
    bundle, report = fake_bundle(), {"artifact_sha256": "digest"}
    monkeypatch.setattr(paper, "read_candidate", lambda *a: (bundle, report))
    monkeypatch.setattr(paper, "candidate_path", lambda *a: tmp_path)
    monkeypatch.setattr(paper, "get_settings", lambda: SimpleNamespace(ml=SimpleNamespace(model_dir=tmp_path)))
    legacy = tmp_path/"ml_model.joblib"
    legacy.write_bytes(b"unchanged legacy")
    with pytest.raises(ValueError, match="forward-paper"):
        paper.promote("a"*32)
    assert not (tmp_path/"outcome-active.json").exists()
    conn = paper.connection(tmp_path)
    try:
        for day in range(30):
            for side in contract.SIDES:
                for i in range(10):
                    stamp = (datetime.now(UTC)-timedelta(days=30-day)).replace(hour=4, minute=i*3, second=0, microsecond=0)
                    outcome = {"timestamp": stamp.isoformat(), "side": side,
                               "exit_timestamp": (stamp+timedelta(minutes=3)).isoformat(),
                               "net_return": .02 if i < 8 else -.01, "session": str(stamp.date())}
                    conn.execute("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?)",
                                 (side, stamp.isoformat(), (stamp+timedelta(seconds=65)).isoformat(),
                                  (stamp+timedelta(minutes=2)).isoformat(), side, "digest", "{}",
                                  "settled", json.dumps(outcome)))
        conn.commit()
    finally:
        conn.close()
    result = paper.promote("a"*32)
    assert result["forward"]["passed"]
    assert legacy.read_bytes() == b"unchanged legacy"
    assert (tmp_path/"outcome-active.json").is_file()


def test_dashboard_adapter_uses_the_same_side_probabilities(monkeypatch):
    bundle, report = fake_bundle(), {"artifact_sha256": "digest"}
    df = contract.frame_features(raw_frame(sessions=1, bars=130))
    now = df.timestamp.iloc[-1].to_pydatetime()+timedelta(seconds=65)
    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    monkeypatch.setattr(adapter, "datetime", FrozenClock)
    monkeypatch.setattr(adapter, "active_manifest", lambda: {
        "candidate_id": bundle["candidate_id"], "artifact_sha256": "digest",
        "expires_at": (now+timedelta(days=1)).isoformat(),
    })
    monkeypatch.setattr(adapter, "read_candidate", lambda *a: (bundle, report))
    model = adapter.OutcomeResearchModel()
    result = model.predict(df, "TEST", "minute")
    assert result["action"] == "BUY"
    assert result["confidence"] == pytest.approx(contract.probabilities(bundle["sides"]["BUY"], df.tail(1))[0])
    assert result["expected_net_return_pct"] > 0
    assert model.predict(df, "OTHER", "minute")["status"] == "outside_outcome_training_scope"


def test_invalid_active_pointer_does_not_fall_back_to_legacy(tmp_path, monkeypatch):
    from trading_agent.analysis import research_engine
    settings = SimpleNamespace(ml=SimpleNamespace(model_dir=tmp_path))
    monkeypatch.setattr(research_engine, "get_settings", lambda: settings)
    monkeypatch.setattr(adapter, "get_settings", lambda: settings)
    (tmp_path/"outcome-active.json").write_text("broken json")
    assert research_engine.model_for_research().status == "outcome_unavailable"


def test_promoted_evidence_only_applies_to_matching_ml_only_policy(monkeypatch):
    from dataclasses import asdict
    from trading_agent.analysis.outcome_validation import estimate
    now = datetime.now(UTC)
    expires = (now+timedelta(days=1)).isoformat()
    manifest = {"candidate_id": "a"*32, "expires_at": expires, "historical": {"BUY": {"trades": 300}}}
    monkeypatch.setattr(adapter, "active_manifest", lambda: manifest)
    row = {"action": "BUY", "ml": {"action": "BUY", "model_kind": "outcome",
           "candidate_id": "a"*32, "policy": asdict(contract.POLICY), "confidence": .8,
           "expected_net_return_pct": .5, "expires_at": expires}}
    assert estimate(row, "ml_only", 2, now)["status"] == "qualified_research"
    assert estimate(row, "rule_ml_combined", 2, now)["win_probability"] is None
    assert estimate(row, "ml_only", 3, now)["win_probability"] is None
    row["ml"]["candidate_id"] = "b"*32
    assert estimate(row, "ml_only", 2, now)["win_probability"] is None
