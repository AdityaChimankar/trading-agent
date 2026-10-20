"""Restored hybrid dashboard contracts, deterministic and without provider calls."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from trading_agent.analysis import research_engine as engine
from trading_agent.analysis import research_jobs as jobs_module
from trading_agent.analysis.research_news import NewsSentiment, usable_sentiment
from trading_agent.core.config import PaperRiskSettings
from trading_agent.strategy.research_features import FEATURES, ResearchModel, prepare_research_frame


@pytest.mark.parametrize("rule,ml,expected", [
    ("BUY", "BUY", "BUY"), ("SELL", "SELL", "SELL"), ("BUY", "SELL", "HOLD"),
    ("SELL", "BUY", "HOLD"), ("HOLD", "BUY", "BUY"), ("SELL", "HOLD", "SELL"),
    ("HOLD", "HOLD", "HOLD"),
])
def test_hybrid_conflicts_abstentions_and_agreements(rule, ml, expected):
    result = engine.combine(rule, 0.8, {"action": ml, "confidence": 0.9, "status": "available"})
    assert result[0] == expected
    if rule != ml and expected != "HOLD":
        assert result[1] < 0.6


def test_missing_ml_is_not_synthetic_agreement():
    assert engine.combine("BUY", 0.8, {"status": "missing"})[0] == "HOLD"
    assert engine.combine("BUY", 0.8, {"status": "missing"}, "rule_only")[0] == "BUY"


def test_completed_candles_exclude_current_and_future():
    now = datetime(2026, 10, 20, 5, tzinfo=UTC)
    data = pd.DataFrame({"timestamp": [now - timedelta(minutes=2), now, now + timedelta(minutes=1)]})
    frame = engine.completed_frame(SimpleNamespace(read_latest=lambda *a, **kw: data), "TEST", "minute", now)
    assert len(frame) == 1


def test_universe_is_deduplicated_not_truncated(tmp_path, monkeypatch):
    path = tmp_path / "symbols.txt"
    path.write_text("# comment\n" + "\n".join(f"SYM{i}" for i in range(2319)) + "\nSYM1", encoding="utf-8")
    monkeypatch.setattr(engine, "SYMBOLS_TXT", path)
    assert len(engine.universe()) == 2319


def test_features_are_causal_and_match_legacy_schema(sample_ohlcv_data):
    full = prepare_research_frame(sample_ohlcv_data)
    prefix = prepare_research_frame(sample_ohlcv_data.iloc[:250])
    np.testing.assert_allclose(full[FEATURES].iloc[249], prefix[FEATURES].iloc[-1])
    tr = pd.concat([sample_ohlcv_data.high - sample_ohlcv_data.low,
                    (sample_ohlcv_data.high - sample_ohlcv_data.close.shift()).abs(),
                    (sample_ohlcv_data.low - sample_ohlcv_data.close.shift()).abs()], axis=1).max(axis=1)
    assert full.atr.iloc[-1] == pytest.approx(tr.tail(14).mean())


class ReorderedClassifier:
    classes_ = np.array([2, 0, 1])

    def predict_proba(self, frame):
        return np.array([[0.8, 0.1, 0.1]])


def test_model_maps_probabilities_by_classes_and_rejects_wrong_interval(tmp_path, sample_ohlcv_data):
    import joblib
    path = tmp_path / "model.joblib"
    joblib.dump({"model": ReorderedClassifier(), "feature_columns": FEATURES}, path)
    model = ResearchModel(path)
    df = prepare_research_frame(sample_ohlcv_data)
    assert model.predict(df, "TEST", "minute")["action"] == "SELL"
    assert model.predict(df, "TEST", "5minute")["status"] == "interval_mismatch"
    df.loc[df.index[-1], "rsi"] = np.nan
    assert model.predict(df, "TEST", "minute")["status"] == "invalid_features"


def test_incompatible_model_is_not_zero_filled(tmp_path):
    import joblib
    path = tmp_path / "model.joblib"
    joblib.dump({"model": ReorderedClassifier(), "feature_columns": ["unknown"]}, path)
    assert ResearchModel(path).status == "incompatible_features"


def test_windows_loss_pickle_alias_is_resolved_without_rewriting(tmp_path, monkeypatch):
    import sys
    import sklearn._loss._loss as loss
    from trading_agent.strategy.research_features import load_legacy_bundle
    path = tmp_path / "windows-loss.joblib"
    original = b"c_loss\nCyHalfMultinomialLoss\n."
    path.write_bytes(original)
    monkeypatch.delitem(sys.modules, "_loss", raising=False)
    assert load_legacy_bundle(path) is loss.CyHalfMultinomialLoss
    assert path.read_bytes() == original


def test_version_mismatch_is_rejected_not_just_warned(tmp_path, monkeypatch):
    import joblib
    import warnings
    from sklearn.exceptions import InconsistentVersionWarning
    path = tmp_path / "model.joblib"
    path.touch()
    def mismatched(*args):
        warnings.warn(InconsistentVersionWarning(estimator_name="Example",
                      current_sklearn_version="1.5.2", original_sklearn_version="0.1"))
    monkeypatch.setattr(joblib, "load", mismatched)
    assert ResearchModel(path).status == "incompatible_version"


def test_unrelated_missing_pickle_modules_are_not_remapped(tmp_path, monkeypatch):
    import joblib
    from trading_agent.strategy.research_features import load_legacy_bundle
    def missing(*args):
        raise ModuleNotFoundError("unknown", name="unrelated_module")
    monkeypatch.setattr(joblib, "load", missing)
    with pytest.raises(ModuleNotFoundError, match="unknown"):
        load_legacy_bundle(tmp_path / "missing.joblib")


def candidate(symbol, side="BUY", stale=False):
    return {"symbol": symbol, "action": side, "score": 75, "stale": stale,
            "price": 100.0, "atr": 2.0, "room_to_target": 12.0}


def test_directional_brackets_and_shared_basket_limits():
    rows = [candidate(f"S{i}", "BUY" if i % 2 else "SELL") for i in range(12)]
    result = engine.size_opportunities(rows, 100000, [], PaperRiskSettings())
    assert sum(row["plan"]["position_value"] for row in result) <= 50000
    assert sum(row["plan"]["risk_amount"] for row in result) <= 6000
    assert sum(row["plan"]["quantity"] > 0 for row in result) <= 10
    for row in result:
        plan = row["plan"]
        if row["action"] == "BUY":
            assert plan["stop_loss"] < 100 < plan["take_profit"]
        else:
            assert plan["take_profit"] < 100 < plan["stop_loss"]


def test_held_stale_and_hold_rows_get_no_size():
    rows = [candidate("HELD"), candidate("STALE", stale=True), candidate("HOLD", side="HOLD")]
    positions = [{"symbol": "HELD", "position_value": 10000, "risk_amount": 300}]
    result = engine.size_opportunities(rows, 100000, positions, PaperRiskSettings())
    assert all(row["plan"]["blocked"] and row["plan"]["quantity"] == 0 for row in result)


def test_news_must_not_come_from_future_or_stale_analysis():
    now = datetime(2026, 10, 20, 5, tzinfo=UTC)
    for timestamp in (now + timedelta(minutes=1), now - timedelta(days=3)):
        assert usable_sentiment({"analysis": {"analyzed_at": timestamp.isoformat()}}, now) is None
    assert usable_sentiment({"analysis": {"analyzed_at": now.isoformat(), "score": 0.5}}, now)["score"] == 0.5


@pytest.mark.parametrize("score", [float("nan"), float("inf"), 1.1, -1.1])
def test_llm_sentiment_rejects_invalid_scores(score):
    with pytest.raises(ValueError):
        NewsSentiment(score=score, confidence=0.8, rationale="test")


def test_scan_counts_all_symbols_and_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path, paper=PaperRiskSettings()))
    monkeypatch.setattr(jobs_module, "model_for_research", lambda: SimpleNamespace(status="loaded", warning="test"))
    monkeypatch.setattr(jobs_module, "get_open_positions", lambda: [])
    monkeypatch.setattr(jobs_module, "get_connection", lambda: SimpleNamespace(
        execute=lambda *args: SimpleNamespace(fetchone=lambda: [0]), close=lambda: None))

    def analyze(symbol, *args):
        if symbol == "MISSING":
            raise ValueError("insufficient_history")
        if symbol == "BROKEN":
            raise OSError("test storage failure")
        return candidate(symbol), None

    monkeypatch.setattr(jobs_module, "analyze_symbol", analyze)
    jobs = jobs_module.ResearchJobs()
    jobs.state.update(status="running", total=3)
    jobs.run(["GOOD", "MISSING", "BROKEN"], {"capital": 100000, "interval": "minute", "mode": "rule_ml_combined", "risk_pct": 1, "reward_risk": 2})
    snapshot = jobs.snapshot()
    assert snapshot["processed"] == 3 and snapshot["evaluated"] == 1
    assert len(snapshot["skipped"]) == 1 and len(snapshot["errors"]) == 1
    assert jobs.path.exists()
