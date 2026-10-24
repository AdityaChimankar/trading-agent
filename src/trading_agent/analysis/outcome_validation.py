"""Versioned rule evidence; probabilities never inferred from legacy ML scores."""
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import numpy as np
from filelock import FileLock
from sklearn.linear_model import LogisticRegression

from trading_agent.core.config import get_settings
from trading_agent.core.signals import evaluate_signals
from trading_agent.research.trade_outcomes import TradePolicy, replay
from trading_agent.storage.parquet_store import get_parquet_store, ParquetStore
from trading_agent.strategy.research_features import prepare_research_frame

VERSION = "rule-brackets-v1"
SECONDS = {"minute": 60, "5minute": 300, "15minute": 900}


def evidence_path(symbol, interval):
    ParquetStore._validate_key(symbol)
    if interval not in SECONDS:
        raise ValueError("Outcome validation supports intraday intervals only")
    return get_settings().data_dir / "research" / "validation" / f"{symbol}-{interval}.json"


def summary(trades):
    n = len(trades)
    if not n:
        return {"trades": 0, "win_rate": None, "win_rate_interval": None, "mean_net_return_pct": None}
    returns = np.array([t["net_return"] for t in trades])
    p, z = float((returns > 0).mean()), 1.96
    center = (p + z*z/(2*n)) / (1 + z*z/n)
    half = z * np.sqrt(p*(1-p)/n + z*z/(4*n*n)) / (1+z*z/n)
    return {"trades": n, "win_rate": p, "win_rate_interval": [center-half, center+half],
            "mean_net_return_pct": float(returns.mean()*100),
            "sessions": len({t["session"] for t in trades})}


def validate_frame(symbol, interval, raw, policy, now=None):
    """Fixed rules; oldest 60% reference, next 20% calibration, last 20% holdout.

    No rule threshold search and no reuse of holdout for fitting. Repeated user
    experimentation can still overfit this holdout; forward paper testing is needed.
    """
    now = now or datetime.now(UTC)
    if len(raw) < 1000:
        raise ValueError("At least 1,000 completed candles are required")
    df = prepare_research_frame(raw)
    actions = evaluate_signals(df.rsi.to_numpy(), df.adx.to_numpy(), df.pattern_code.to_numpy())
    scores = np.minimum(.95, .5 + np.minimum(df.adx.to_numpy(), 50)/200 + .1*(df.pattern_code.to_numpy() != 0))
    a, b = int(len(df)*.6), int(len(df)*.8)
    reference, calibration, holdout = [
        replay(df, actions, scores, start, end, policy, SECONDS[interval])
        for start, end in [(0, a), (a, b), (b, len(df))]
    ]
    result = {"version": VERSION, "symbol": symbol, "interval": interval, "mode": "rule_only",
              "policy": asdict(policy), "created_at": now.isoformat(),
              "expires_at": min(now+timedelta(days=7), df.timestamp.iloc[-1].to_pydatetime()+timedelta(days=7)).isoformat(),
              "data_start": df.timestamp.iloc[0].isoformat(), "data_end": df.timestamp.iloc[-1].isoformat(),
              "windows": {"reference_end": df.timestamp.iloc[a-1].isoformat(),
                          "calibration_start": df.timestamp.iloc[a].isoformat(),
                          "calibration_end": df.timestamp.iloc[b-1].isoformat(),
                          "holdout_start": df.timestamp.iloc[b].isoformat()},
              "reference": summary(reference), "calibration": summary(calibration),
              "holdout": summary(holdout), "status": "insufficient_evidence", "sides": {},
              "limitations": "Fixed-rule research, not hybrid/ML validation. Costs are assumptions. No order-book fills, corporate-action adjustment or official holiday calendar. Wilson intervals assume independent trades; session bootstrap partially addresses dependence. Repeated holdout inspection can overfit. No guarantee of future returns."}
    for side in ("BUY", "SELL"):
        cal = [t for t in calibration if t["side"] == side]
        test = [t for t in holdout if t["side"] == side]
        item = {"status": "insufficient_evidence", **summary(test)}
        result["sides"][side] = item
        if len(cal) < 100 or len(test) < 100 or min(sum(t["win"] for t in cal), sum(not t["win"] for t in cal)) < 20:
            continue
        clf = LogisticRegression(C=1.0, random_state=0)
        clf.fit(np.array([[t["score"]] for t in cal]), [t["win"] for t in cal])
        y = np.array([t["win"] for t in test])
        probs = clf.predict_proba(np.array([[t["score"]] for t in test]))[:, 1]
        base = np.mean([t["win"] for t in cal])
        brier, baseline = float(np.mean((probs-y)**2)), float(np.mean((base-y)**2))
        # Resample sessions, not individual trades, for an expectancy guard.
        sessions = sorted({t["session"] for t in test})
        groups = [np.array([t["net_return"] for t in test if t["session"] == day]) for day in sessions]
        rng = np.random.default_rng(0)
        means = [float(np.concatenate([groups[j] for j in rng.integers(0, len(groups), len(groups))]).mean())
                 for _ in range(1000)]
        lower = float(np.quantile(means, .025))
        reliability = []
        for lo in np.arange(0, 1, .2):
            mask = (probs >= lo) & (probs < lo+.2)
            if mask.any():
                reliability.append({"count": int(mask.sum()), "predicted": float(probs[mask].mean()),
                                    "observed": float(y[mask].mean())})
        sufficient = len(sessions) >= 10
        passed = sufficient and lower > 0 and brier < baseline
        item.update(status="qualified_research" if passed else "failed_validation",
                    brier=brier, baseline_brier=baseline, reliability=reliability,
                    mean_net_return_lower_95_pct=lower*100,
                    coefficient=float(clf.coef_[0, 0]), intercept=float(clf.intercept_[0]),
                    score_min=float(min(t["score"] for t in cal)), score_max=float(max(t["score"] for t in cal)),
                    mean_win=float(np.mean([t["net_return"] for t in cal if t["win"]])),
                    mean_loss=float(np.mean([t["net_return"] for t in cal if not t["win"]])))
    if any(v["status"] == "qualified_research" for v in result["sides"].values()):
        result["status"] = "qualified_research"
    elif any(v["status"] == "failed_validation" for v in result["sides"].values()):
        result["status"] = "failed_validation"
    return result


def validate_symbol(symbol, interval="minute", reward_risk=2.0, cost_bps=10.0, slippage_bps=2.0, horizon=24):
    from trading_agent.analysis.research_engine import completed_frame
    path = evidence_path(symbol, interval)
    now = datetime.now(UTC)
    raw = completed_frame(get_parquet_store(), symbol, interval, now)
    result = validate_frame(symbol, interval, raw, TradePolicy(horizon=horizon, reward_risk=reward_risk,
                            cost_bps=cost_bps, slippage_bps=slippage_bps), now)
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path)+".lock"):
        temporary = path.with_name(f".{uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(result, allow_nan=False), encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return result


def read_evidence(symbol, interval):
    try:
        return json.loads(evidence_path(symbol, interval).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "not_validated"}


def estimate(row, mode, reward_risk, now):
    unavailable = {"status": "unvalidated", "win_probability": None, "expected_net_return_pct": None,
                   "reason": "No independent evidence for this strategy"}
    ml = row.get("ml", {})
    if mode == "ml_only" and ml.get("model_kind") == "outcome":
        from trading_agent.strategy.outcome_contract import POLICY
        from trading_agent.strategy.outcome_model import active_manifest
        try:
            active = active_manifest()
            action = row["action"]
            if (active is None or ml["candidate_id"] != active["candidate_id"] or action not in ("BUY", "SELL")
                    or ml["action"] != action or ml["policy"] != asdict(POLICY)
                    or reward_risk != POLICY.reward_risk or active["expires_at"] != ml["expires_at"]
                    or datetime.fromisoformat(ml["expires_at"]) <= now):
                return {**unavailable, "reason": "Outcome artifact, direction, policy or evidence does not match"}
            p, expected = ml["confidence"], ml["expected_net_return_pct"]
            if not np.isfinite([p, expected]).all() or p <= .60:
                return unavailable
            return {"status": "qualified_research", "win_probability": p, "expected_net_return_pct": expected,
                    "sample_count": active["historical"][action]["trades"],
                    "expires_at": active["expires_at"], "candidate_id": ml["candidate_id"],
                    "reason": "Promoted outcome ML: historical and prospective paper gates passed; future profits are not guaranteed"}
        except (KeyError, TypeError, ValueError, OSError):
            return unavailable
    if mode != "rule_only":
        return {**unavailable, "reason": "Only promoted outcome ML in ML-only mode has model evidence; legacy/hybrid probabilities remain unvalidated"}
    try:
        report = read_evidence(row["symbol"], row["interval"])
        if (report["version"] != VERSION or report["symbol"] != row["symbol"] or report["interval"] != row["interval"]
                or report["policy"] != asdict(TradePolicy(reward_risk=reward_risk))
                or datetime.fromisoformat(report["expires_at"]) <= now
                or datetime.fromisoformat(report["data_end"]) >= datetime.fromisoformat(row["timestamp"])):
            return {**unavailable, "reason": "Evidence expired or trade policy/data window does not match"}
        item = report["sides"][row["action"]]
        if item["status"] != "qualified_research":
            return {**unavailable, "status": item["status"], "reason": "Independent sample/expectancy/calibration gates not passed",
                    "sample_count": item["trades"]}
        score = row["rule"]["confidence"]
        if not item["score_min"] <= score <= item["score_max"]:
            return {**unavailable, "reason": "Signal lies outside calibration score range"}
        p = float(1/(1+np.exp(-np.clip(item["coefficient"]*score+item["intercept"], -30, 30))))
        expected = 100*(p*item["mean_win"]+(1-p)*item["mean_loss"])
        if not np.isfinite([p, expected]).all():
            return unavailable
        return {"status": "qualified_research", "win_probability": p, "expected_net_return_pct": expected,
                "sample_count": item["trades"], "win_rate_interval": item["win_rate_interval"],
                "expires_at": report["expires_at"], "reason": "Historical rule-only estimate; not a guaranteed outcome"}
    except (KeyError, TypeError, ValueError, OverflowError):
        return unavailable
