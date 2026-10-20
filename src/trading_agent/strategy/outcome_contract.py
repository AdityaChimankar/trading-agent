"""One versioned contract for side-specific outcome training, paper and inference."""
import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from trading_agent.research.trade_outcomes import TradePolicy
from trading_agent.strategy.research_features import FEATURES, prepare_research_frame

SCHEMA = "buy-sell-outcome-v1"
SIDES = ("BUY", "SELL")
FEATURE_COLUMNS = FEATURES + ["time_sin", "time_cos", "dist_ma50", "volatility20"]
# One full decision-latency bar makes a future entry recordable before its open.
POLICY = TradePolicy(entry_delay_bars=2)
THRESHOLDS = (.60, .65, .70, .75, .80, .85, .90)
HISTORICAL_MIN_TRADES, HISTORICAL_MIN_SESSIONS = 100, 10
FORWARD_MIN_TRADES, FORWARD_MIN_SESSIONS = 100, 20


def contract_hash():
    root = Path(__file__).resolve().parents[1]
    files = [Path(__file__), root/"strategy/research_features.py",
             root/"research/trade_outcomes.py", root/"research/walk_forward_optimizer.py",
             root/"strategy/train_outcome_models.py", root/"strategy/outcome_model.py",
             root/"strategy/outcome_paper.py"]
    return hashlib.sha256(b"".join(p.read_bytes() for p in files)).hexdigest()


def frame_features(raw):
    df = raw.copy()
    df["timestamp"] = pd.to_datetime(df.timestamp, utc=True, format="mixed")
    df = df.sort_values("timestamp").drop_duplicates("timestamp", keep="last").reset_index(drop=True)
    if (df.timestamp.isna().any() or (df.timestamp.dt.second != 0).any() or
            (df.timestamp.dt.microsecond != 0).any() or (df.timestamp.dt.nanosecond != 0).any()):
        raise ValueError("Outcome models require minute-aligned UTC candle timestamps")
    df = prepare_research_frame(df)
    local = df.timestamp.dt.tz_convert("Asia/Kolkata")
    minute = local.dt.hour*60+local.dt.minute
    df["time_sin"] = np.sin(2*np.pi*minute/1440)
    df["time_cos"] = np.cos(2*np.pi*minute/1440)
    df["dist_ma50"] = df.close/df.ma50-1
    df["volatility20"] = df.close.pct_change(fill_method=None).rolling(20).std()
    return df


def eligible_features(df):
    local = df.timestamp.dt.tz_convert("Asia/Kolkata")
    minute = local.dt.hour*60+local.dt.minute
    dates = local.dt.date
    gaps = ((df.timestamp.diff().dt.total_seconds() > 90) & (dates == dates.shift())).rolling(20).max().fillna(1)
    return (np.isfinite(df[FEATURE_COLUMNS].to_numpy(dtype=float)).all(axis=1) &
            (df.atr > 0) & (df.close > 0) & (df.volume > 0) &
            ((df.close*df.volume).rolling(20).mean() >= 100000) & (gaps == 0) &
            (local.dt.weekday < 5) & (minute >= 555) & (minute < 918))


def probabilities(side_model, df):
    x = df[FEATURE_COLUMNS]
    positive = list(side_model["model"].classes_).index(1)
    raw = side_model["model"].predict_proba(x)[:, positive]
    logit = np.log(np.clip(raw, 1e-6, 1-1e-6)/np.clip(1-raw, 1e-6, 1))
    return side_model["calibrator"].predict_proba(logit.reshape(-1, 1))[:, 1]


def decisions(buy, sell, thresholds):
    """Identical arbitration in test, prospective paper and dashboard.

    Both sides passing is a conflict, not two simultaneous trades.
    """
    b, s = buy > thresholds["BUY"], sell > thresholds["SELL"]
    return np.where(b & ~s, 1, np.where(s & ~b, 2, 0))


def metrics(trades, forward=False):
    minimum = FORWARD_MIN_TRADES if forward else HISTORICAL_MIN_TRADES
    sessions_min = FORWARD_MIN_SESSIONS if forward else HISTORICAL_MIN_SESSIONS
    n = len(trades)
    sessions = sorted({t["session"] for t in trades})
    result = {"trades": n, "sessions": len(sessions), "win_rate": None,
              "win_lower_95": None, "mean_net_return_pct": None,
              "mean_lower_95_pct": None, "passed": False,
              "required_win_rate": .60, "minimum_trades": minimum, "minimum_sessions": sessions_min}
    if not n:
        return result
    r = np.asarray([t["net_return"] for t in trades], dtype=float)
    if not np.isfinite(r).all():
        raise ValueError("Non-finite outcome")
    p, z = float((r > 0).mean()), 1.96
    lower = (p+z*z/(2*n)-z*np.sqrt(p*(1-p)/n+z*z/(4*n*n)))/(1+z*z/n)
    # Session blocks preserve contemporaneous cross-symbol dependence.
    groups = [np.asarray([t["net_return"] for t in trades if t["session"] == session]) for session in sessions]
    rng = np.random.default_rng(42)
    samples = [np.concatenate([groups[j] for j in rng.integers(0, len(groups), len(groups))])
               for _ in range(1000)]
    mean_lower = float(np.quantile([a.mean() for a in samples], .025))
    block_win_lower = float(np.quantile([(a > 0).mean() for a in samples], .025))
    result.update(win_rate=p, win_lower_95=float(lower), session_win_lower_95=block_win_lower,
                  mean_net_return_pct=float(r.mean()*100), mean_lower_95_pct=mean_lower*100,
                  passed=bool(n >= minimum and len(sessions) >= sessions_min and
                              p > .60 and lower > .60 and block_win_lower > .60 and mean_lower > 0))
    return result


def atomic_json(path, value):
    from uuid import uuid4
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, allow_nan=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def check_bundle(bundle):
    if (bundle.get("schema") != SCHEMA or bundle.get("contract_hash") != contract_hash() or
            bundle.get("features") != FEATURE_COLUMNS or bundle.get("policy") != asdict(POLICY) or
            bundle.get("interval") != "minute"):
        raise ValueError("Outcome model contract differs from runtime; retrain/revalidate")
    if set(bundle["sides"]) != set(SIDES):
        raise ValueError("Both side models are required")
    if any(not .60 <= bundle["thresholds"][s] <= 1.01 for s in SIDES):
        raise ValueError("Invalid frozen threshold")
    datetime.fromisoformat(bundle["created_at"]).astimezone(UTC)
