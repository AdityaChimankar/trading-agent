"""ML feature-drift monitoring with automatic withdrawal of a drifted model.

1. Baseline: ``build_baseline`` computes decile edges and proportions for each
   model feature over a reference period of stored history (ideally the
   model's training window) and saves them to ``models/feature_baseline.json``.
2. Live logging: every research prediction appends the exact feature vector
   it used to ``data/ml_monitoring/features-<IST date>.jsonl``.
3. Report: ``drift_report`` compares recent live vectors with the baseline
   per feature using the Population Stability Index (PSI) and the two-sample
   Kolmogorov-Smirnov statistic.
4. Fallback: when ``drift_features_to_fallback`` or more features exceed
   ``psi_drift``, the monitor records a fallback. ``model_for_research`` then
   loads ``ml_model.previous.joblib`` if one exists, or withdraws ML, so the
   hybrid mode returns HOLD rather than trading on a drifted model. Clearing
   the fallback is an explicit operator action.

PSI < 0.1 stable, 0.1-0.25 moderate shift, > 0.25 significant shift.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from trading_agent.core.config import get_settings
from trading_agent.core.logging_config import get_logger
from trading_agent.strategy.research_features import FEATURES

logger = get_logger(__name__)
IST = ZoneInfo("Asia/Kolkata")
BINS = 10
MIN_LIVE_SAMPLES = 200
_log_lock = threading.Lock()


def _monitor_dir() -> Path:
    path = get_settings().data_dir / "ml_monitoring"
    path.mkdir(parents=True, exist_ok=True)
    return path


def baseline_path() -> Path:
    return Path(get_settings().ml.model_dir) / "feature_baseline.json"


def _status_path() -> Path:
    return _monitor_dir() / "status.json"


# --- statistics ------------------------------------------------------------------

def psi(expected: np.ndarray, actual: np.ndarray, eps: float = 1e-4) -> float:
    """Population Stability Index between two bin-proportion vectors."""
    e = np.clip(np.asarray(expected, dtype=float), eps, None)
    a = np.clip(np.asarray(actual, dtype=float), eps, None)
    e, a = e / e.sum(), a / a.sum()
    return float(np.sum((a - e) * np.log(a / e)))


def _proportions(values: np.ndarray, edges: list[float]) -> np.ndarray:
    # Open-ended outer bins: live values beyond the baseline range still count.
    inner = np.asarray(edges[1:-1], dtype=float)
    counts = np.bincount(np.searchsorted(inner, values, side="right"), minlength=len(edges) - 1)
    return counts / max(1, counts.sum())


def ks_statistic(sample_a: np.ndarray, sample_b: np.ndarray) -> float:
    from scipy.stats import ks_2samp

    return float(ks_2samp(sample_a, sample_b).statistic)


# --- baseline ----------------------------------------------------------------------

def baseline_from_frame(features: pd.DataFrame, source: str) -> dict:
    out = {"created_at": datetime.now(UTC).isoformat(), "source": source, "samples": int(len(features)),
           "features": {}}
    for name in FEATURES:
        values = features[name].to_numpy(dtype=float)
        values = values[np.isfinite(values)]
        if len(values) < BINS * 20:
            continue
        edges = np.unique(np.quantile(values, np.linspace(0, 1, BINS + 1)))
        if len(edges) < 3:  # near-constant feature (e.g. a pattern code); compare by value
            edges = np.unique(np.r_[values.min() - 0.5, np.unique(values) + 0.5])
        rng = np.random.default_rng(0)
        reference = rng.choice(values, size=min(2000, len(values)), replace=False)
        out["features"][name] = {"edges": edges.tolist(),
                                 "proportions": _proportions(values, edges.tolist()).tolist(),
                                 "reference_sample": np.round(reference, 6).tolist()}
    return out


def build_baseline(symbols: list[str], interval: str = "minute", end: datetime | None = None,
                   bars_per_symbol: int = 3000) -> dict:
    """Feature distribution over history ending at ``end`` (use the training cutoff)."""
    from trading_agent.analysis.research_engine import completed_frame
    from trading_agent.storage.parquet_store import get_parquet_store
    from trading_agent.strategy.research_features import prepare_research_frame

    end = end or datetime.now(UTC)
    frames = []
    for symbol in symbols:
        frame = completed_frame(get_parquet_store(), symbol, interval, end)
        if len(frame) < 100:
            continue
        frames.append(prepare_research_frame(frame.tail(bars_per_symbol))[FEATURES])
    if not frames:
        raise ValueError("No stored history to build a baseline from")
    baseline = baseline_from_frame(pd.concat(frames, ignore_index=True),
                                   f"{len(frames)} symbols, {interval}, history to {end.isoformat()}")
    path = baseline_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(baseline), encoding="utf-8")
    return baseline


def load_baseline() -> dict | None:
    path = baseline_path()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


# --- live logging --------------------------------------------------------------------

def log_live_features(symbol: str, interval: str, row: pd.Series) -> None:
    """Append the exact feature vector a live prediction used. Never raises."""
    try:
        values = {name: float(row[name]) for name in FEATURES}
        if not all(np.isfinite(list(values.values()))):
            return
        now = datetime.now(UTC)
        record = {"at": now.isoformat(), "symbol": symbol, "interval": interval, **values}
        path = _monitor_dir() / f"features-{now.astimezone(IST).date().isoformat()}.jsonl"
        with _log_lock, path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except Exception as error:
        logger.warning("feature_log_failed", symbol=symbol, error=str(error))


def read_live_features(days: int = 3) -> pd.DataFrame:
    today = datetime.now(IST).date()
    rows = []
    for offset in range(days):
        path = _monitor_dir() / f"features-{(today - timedelta(days=offset)).isoformat()}.jsonl"
        if path.exists():
            rows.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line)
    return pd.DataFrame(rows)


# --- report and fallback ---------------------------------------------------------------

def drift_report(live: pd.DataFrame | None = None, baseline: dict | None = None, days: int = 3) -> dict:
    settings = get_settings().monitoring
    baseline = baseline if baseline is not None else load_baseline()
    live = live if live is not None else read_live_features(days)
    base = {"generated_at": datetime.now(UTC).isoformat(), "live_samples": int(len(live)),
            "thresholds": {"psi_warn": settings.psi_warn, "psi_drift": settings.psi_drift,
                           "features_to_fallback": settings.drift_features_to_fallback}}
    if baseline is None:
        return {**base, "status": "no_baseline", "features": [],
                "message": "Build a baseline: python -m trading_agent.strategy.model_monitoring baseline"}
    if len(live) < MIN_LIVE_SAMPLES:
        return {**base, "status": "insufficient_live_data", "features": [], "baseline_source": baseline["source"],
                "message": f"Need {MIN_LIVE_SAMPLES} logged predictions, have {len(live)}"}
    features = []
    for name, spec in baseline["features"].items():
        values = live[name].to_numpy(dtype=float)
        values = values[np.isfinite(values)]
        score = psi(spec["proportions"], _proportions(values, spec["edges"]))
        state = "drift" if score > settings.psi_drift else "warn" if score > settings.psi_warn else "ok"
        features.append({"feature": name, "psi": round(score, 4),
                         "ks": round(ks_statistic(np.asarray(spec["reference_sample"]), values), 4),
                         "live_mean": round(float(values.mean()), 6), "state": state})
    features.sort(key=lambda f: -f["psi"])
    drifted = [f["feature"] for f in features if f["state"] == "drift"]
    status = ("drift" if len(drifted) >= settings.drift_features_to_fallback
              else "warn" if drifted or any(f["state"] == "warn" for f in features) else "ok")
    return {**base, "status": status, "drifted_features": drifted, "features": features,
            "baseline_source": baseline["source"]}


def fallback_state() -> dict:
    path = _status_path()
    if not path.exists():
        return {"active": False}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {"active": True, "reason": "Monitoring status unreadable"}  # fail closed


def evaluate_and_apply(report: dict | None = None) -> dict:
    """Run the report; on drift, record a fallback that ``model_for_research`` obeys."""
    report = report or drift_report()
    if report["status"] == "drift" and not fallback_state().get("active"):
        state = {"active": True, "since": datetime.now(UTC).isoformat(),
                 "reason": f"PSI drift on {', '.join(report['drifted_features'])}"}
        _status_path().write_text(json.dumps(state), encoding="utf-8")
        from trading_agent.risk.controls import audit

        audit("ml_drift_fallback", **state)
        logger.warning("ml_drift_fallback", **state)
    return {"report": report, "fallback": fallback_state()}


def clear_fallback(by: str = "operator") -> dict:
    _status_path().write_text(json.dumps({"active": False, "cleared_at": datetime.now(UTC).isoformat(),
                                          "by": by}), encoding="utf-8")
    from trading_agent.risk.controls import audit

    audit("ml_drift_fallback_cleared", by=by)
    return fallback_state()


class DriftWithheldModel:
    """Stand-in while drift fallback is active and no previous model exists."""

    status = "drift_fallback"
    warning = "ML withdrawn: feature drift detected; hybrid decisions hold until cleared"

    def predict(self, df, symbol, interval):
        return {"action": None, "confidence": None, "status": self.status}


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="ML feature-drift monitoring")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("baseline", help="Build the reference distribution")
    b.add_argument("symbols", nargs="*")
    b.add_argument("--watchlist", type=int, default=50, help="Use the first N watchlist symbols")
    b.add_argument("--interval", default="minute")
    b.add_argument("--end", help="ISO timestamp; use the model's training cutoff")
    sub.add_parser("report", help="Print the drift report and apply fallback on drift")
    sub.add_parser("clear", help="Clear an active fallback")
    args = parser.parse_args(argv)
    if args.command == "baseline":
        from trading_agent.analysis.research_engine import universe

        symbols = [s.upper() for s in args.symbols] or universe()[: args.watchlist]
        end = datetime.fromisoformat(args.end) if args.end else None
        result = build_baseline(symbols, args.interval, end)
        print(f"Baseline: {result['samples']} rows, {len(result['features'])} features -> {baseline_path()}")
    elif args.command == "report":
        print(json.dumps(evaluate_and_apply(), indent=2))
    else:
        print(json.dumps(clear_fallback(), indent=2))


if __name__ == "__main__":
    main()
