"""Explicit offline candidate training. Never changes the active model."""
import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import uuid4

import joblib
import numpy as np
import pandas as pd
import sklearn
from filelock import FileLock
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from threadpoolctl import threadpool_limits

from trading_agent.core.config import get_settings
from trading_agent.research.trade_outcomes import outcome_arrays
from trading_agent.storage.parquet_store import get_parquet_store, ParquetStore
from trading_agent.strategy.research_features import load_legacy_bundle
from trading_agent.strategy.outcome_contract import (
    SCHEMA, SIDES, FEATURE_COLUMNS, POLICY, THRESHOLDS, atomic_json, contract_hash,
    decisions, eligible_features, frame_features, metrics, probabilities,
)


def candidate_root():
    return get_settings().ml.model_dir / "outcome_candidates"


def candidate_path(candidate_id):
    if len(candidate_id) != 32 or any(c not in "0123456789abcdef" for c in candidate_id):
        raise ValueError("Invalid candidate ID")
    return candidate_root()/candidate_id


def read_candidate(candidate_id):
    path = candidate_path(candidate_id)
    report = json.loads((path/"report.json").read_text(encoding="utf-8"))
    artifact = path/"model.joblib"
    if hashlib.sha256(artifact.read_bytes()).hexdigest() != report["artifact_sha256"]:
        raise ValueError("Candidate artifact checksum mismatch")
    # Same pinned sklearn contract and Windows/Linux loss-class compatibility.
    bundle = load_legacy_bundle(artifact)  # Trusted locally trained files only.
    from trading_agent.strategy.outcome_contract import check_bundle
    check_bundle(bundle)
    if bundle["candidate_id"] != candidate_id:
        raise ValueError("Candidate identity mismatch")
    return bundle, report


def partitions(data):
    """Shared session boundaries across all symbols; purge complete label horizons."""
    sessions = sorted({str(d) for df in data.values() for d in df.timestamp.dt.tz_convert("Asia/Kolkata").dt.date})
    if len(sessions) < 40:
        raise ValueError("At least 40 distinct stored sessions are required")
    cuts = [pd.Timestamp(sessions[int(len(sessions)*f)], tz="Asia/Kolkata").tz_convert("UTC")
            for f in (.50, .65, .80)]
    return cuts


def partition_mask(df, start, end):
    mask = eligible_features(df)
    if start is not None:
        mask &= df.timestamp >= start
    # Conservative full-horizon purging, even when a barrier touched early.
    label_end = df.timestamp.shift(-(POLICY.horizon+POLICY.entry_delay_bars-1))+pd.Timedelta(minutes=1)
    mask &= label_end.notna()
    if end is not None:
        mask &= label_end < end
    return mask.to_numpy()


def selected_trades(data, labels, predictions, thresholds, start, end):
    result = []
    for symbol, df in data.items():
        buy, sell = predictions[symbol]["BUY"], predictions[symbol]["SELL"]
        choices = decisions(buy, sell, thresholds)
        allowed = partition_mask(df, start, end)
        next_free = 0
        for i in np.flatnonzero(allowed & (choices != 0)):
            if i < next_free:
                continue
            side = "BUY" if choices[i] == 1 else "SELL"
            net, exits = labels[symbol][side]
            if not np.isfinite(net[i]):
                continue
            exit_i = int(exits[i])
            result.append({"symbol": symbol, "side": side, "timestamp": df.timestamp.iloc[i].isoformat(),
                           "exit_timestamp": df.timestamp.iloc[exit_i].isoformat(),
                           "session": str(df.timestamp.iloc[i].tz_convert("Asia/Kolkata").date()),
                           "net_return": float(net[i]), "probability": float(predictions[symbol][side][i])})
            # A later signal is allowed if its delayed entry follows this exit.
            next_free = exit_i
    return result


def fit_data(data, candidate_id, created_at):
    cuts = partitions(data)
    windows = list(zip([None, *cuts], [*cuts, None]))
    labels = {symbol: {side: outcome_arrays(df, side, POLICY) for side in SIDES}
              for symbol, df in data.items()}
    side_models = {}
    for side in SIDES:
        samples = []
        for start, end in windows[:2]:
            chunks, targets, returns = [], [], []
            for symbol, df in data.items():
                net, _ = labels[symbol][side]
                mask = partition_mask(df, start, end) & np.isfinite(net)
                chunks.append(df.loc[mask, FEATURE_COLUMNS])
                targets.extend((net[mask] > 0).astype(int))
                returns.extend(net[mask])
            x, y = pd.concat(chunks, ignore_index=True), np.asarray(targets)
            if len(x) < 300 or min(np.sum(y == 0), np.sum(y == 1)) < 30:
                raise ValueError(f"{side}: insufficient observed examples/classes for training/calibration")
            samples.append((x, y, np.asarray(returns)))
        classifier = HistGradientBoostingClassifier(max_iter=100, max_leaf_nodes=15,
                        min_samples_leaf=50, l2_regularization=5., learning_rate=.05,
                        early_stopping=False, random_state=42)
        classifier.fit(*samples[0][:2])
        raw = classifier.predict_proba(samples[1][0])[:, list(classifier.classes_).index(1)]
        logit = np.log(np.clip(raw, 1e-6, 1-1e-6)/np.clip(1-raw, 1e-6, 1))
        calibrator = LogisticRegression(C=1., random_state=42).fit(logit.reshape(-1, 1), samples[1][1])
        cal_returns = samples[1][2]
        side_models[side] = {"model": classifier, "calibrator": calibrator,
                            "mean_win": float(cal_returns[cal_returns > 0].mean()),
                            "mean_loss": float(cal_returns[cal_returns <= 0].mean())}
    # No test outcomes are inspected while choosing models/calibrators/thresholds.
    predictions = {}
    for symbol, df in data.items():
        mask = eligible_features(df).to_numpy()
        predictions[symbol] = {}
        for side in SIDES:
            p = np.zeros(len(df))
            if mask.any():
                p[mask] = probabilities(side_models[side], df.loc[mask])
            predictions[symbol][side] = p
    thresholds, tuning = {}, {}
    # Freeze each side's threshold on tuning only, then test joint arbitration once.
    for side in SIDES:
        best, threshold, evidence = -np.inf, 1.01, []
        for value in THRESHOLDS:
            trial = {"BUY": 1.01, "SELL": 1.01, side: value}
            trades = selected_trades(data, labels, predictions, trial, cuts[1], cuts[2])
            if len(trades) < 30 or len({t["session"] for t in trades}) < 5:
                continue
            average = np.mean([t["net_return"] for t in trades])
            win = np.mean([t["net_return"] > 0 for t in trades])
            if win > .60 and average > 0 and average > best:
                best, threshold, evidence = average, value, trades
        thresholds[side] = threshold
        tuning[side] = {"threshold": threshold, "trades": len(evidence),
                        "reason": "selected on tuning only" if evidence else "No tuning threshold met 60%/positive expectancy/sample gates"}
    # Freeze the policy before accessing test outcomes for evaluation.
    test = selected_trades(data, labels, predictions, thresholds, cuts[2], None)
    evaluation = {side: metrics([t for t in test if t["side"] == side]) for side in SIDES}
    bundle = {"schema": SCHEMA, "contract_hash": contract_hash(), "candidate_id": candidate_id,
              "created_at": created_at, "features": FEATURE_COLUMNS, "interval": "minute",
              "sklearn_version": sklearn.__version__,
              "policy": asdict(POLICY), "symbols": sorted(data), "sides": side_models,
              "thresholds": thresholds, "test_trades": test,
              "test_end": max(df.timestamp.max() for df in data.values()).isoformat()}
    report = {k: bundle[k] for k in ("schema", "contract_hash", "candidate_id", "created_at", "features", "interval", "policy", "symbols", "thresholds", "test_end")}
    report.update(status="awaiting_forward_paper" if all(v["passed"] for v in evaluation.values()) else "historical_gate_failed",
                  windows={"calibration_start": cuts[0].isoformat(), "tuning_start": cuts[1].isoformat(),
                           "test_start": cuts[2].isoformat()},
                  test=evaluation, tuning=tuning, data_rows={s: len(df) for s, df in data.items()},
                  limitations="Historical holdout excluded from fit/calibration/selection, not proof of market-level pristine data. Existing history was previously inspected with legacy strategies. No portfolio allocation or real execution; survivorship/corporate-action and selection bias remain. Prospective paper confirmation is mandatory.")
    return bundle, report


def train_candidate(symbols):
    symbols = sorted(set(s.strip().upper() for s in symbols))
    if not symbols:
        raise ValueError("Specify the training symbols explicitly")
    for symbol in symbols:
        ParquetStore._validate_key(symbol)
    now = datetime.now(UTC)
    data = {}
    for symbol in symbols:
        raw = get_parquet_store().read_latest(symbol, "minute", n=20000)
        if raw.empty:
            raise ValueError(f"{symbol}: no stored candles")
        raw = raw.copy()
        raw["timestamp"] = pd.to_datetime(raw.timestamp, utc=True, format="mixed")
        df = frame_features(raw.loc[raw.timestamp <= now-pd.Timedelta(minutes=1)])
        if len(df) < 1000:
            raise ValueError(f"{symbol}: insufficient history")
        data[symbol] = df
    cuts = partitions(data)
    test_start = cuts[2]
    test_end = max(df.timestamp.max() for df in data.values())
    root = candidate_root()
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root/"training.lock"), timeout=0):
        ledger_path = root/"reserved_tests.json"
        reservations = json.loads(ledger_path.read_text()) if ledger_path.exists() else []
        # Global dates, not just dataset hashes: changing symbols/config must not
        # rebrand an already inspected market period as an untouched test.
        if any(test_start <= pd.Timestamp(r["end"]) and test_end >= pd.Timestamp(r["start"])
               for r in reservations):
            raise ValueError("This test period is already reserved/evaluated. Collect a genuinely later test period; no retry-on-holdout override is provided.")
        candidate_id = uuid4().hex
        reservation = {"candidate_id": candidate_id, "start": test_start.isoformat(), "end": test_end.isoformat()}
        reservations.append(reservation)
        atomic_json(ledger_path, reservations)  # Reservation survives interrupted/failed runs.
        path = candidate_path(candidate_id)
        path.mkdir()
        atomic_json(path/"report.json", {**reservation, "status": "training", "symbols": symbols, "created_at": now.isoformat()})
        try:
            with threadpool_limits(limits=2):
                bundle, report = fit_data(data, candidate_id, now.isoformat())
            fingerprint = hashlib.sha256()
            for symbol, df in sorted(data.items()):
                fingerprint.update(symbol.encode())
                fingerprint.update(pd.util.hash_pandas_object(df[["timestamp", "open", "high", "low", "close", "volume"]], index=False).to_numpy().tobytes())
            bundle["data_sha256"] = report["data_sha256"] = fingerprint.hexdigest()
            joblib.dump(bundle, path/"model.joblib")
            report["artifact_sha256"] = hashlib.sha256((path/"model.joblib").read_bytes()).hexdigest()
            atomic_json(path/"report.json", report)
            return report
        except Exception as error:
            atomic_json(path/"report.json", {**reservation, "status": "training_failed", "error": str(error)})
            raise
