"""Score stored decisions on what happened afterwards (adapted from TradingAgents' settlement).

Every BUY/SELL row in ``signals`` (rule, ml, llm, hybrid, ...) is priced from the
stored candles of its own interval: entry at the open of the first bar that
starts at or after the signal's timestamp, exit at the close of the last bar
that starts within ``horizon_minutes`` of the entry and before the 15:15 MIS
exit. The return is signed by the action and net of ``cost_bps`` round trip.
When Nifty 50 candles are stored, the index return over the same bars is
subtracted (alpha); otherwise alpha is left empty.

Each signal is scored on its own, so overlapping signals on a symbol are
correlated: the t-stat therefore uses per-session sums. HOLD rows are counted,
not traded. A signal whose window has not finished is ``pending``, one whose
bars are missing is ``unpriced``, and one at or after 15:15 is ``after_cutoff``.
None of these counts as a loss.

This replaces ``scripts/llm_eval.py``'s agreement-with-rules measure for the
question that matters: did the decisions make money after costs? Research only.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from trading_agent.core.config import get_settings
from trading_agent.core.logging_config import get_logger

logger = get_logger(__name__)

IST = "Asia/Kolkata"
EXIT_MINUTE = 15 * 60 + 15
INDEX = "NIFTY50"
CONFIDENCE_BANDS = ((0.0, 0.6, "<0.60"), (0.6, 0.75, "0.60-0.75"), (0.75, 1.01, ">=0.75"))


@dataclass(frozen=True)
class ScoreConfig:
    days: int = 30
    horizon_minutes: int = 60
    cost_bps: float = 14.0  # 10 bps charges + 2 bps slippage per fill, as the strategy search


def load_signals(since: datetime, session=None) -> pd.DataFrame:
    """Signals stored since ``since`` (UTC), oldest first."""
    from sqlalchemy import select

    from trading_agent.storage.db import Signal, get_database

    own = session is None
    session = session or get_database().get_sync_session()
    try:
        rows = session.execute(select(Signal).where(Signal.timestamp >= since).order_by(Signal.timestamp)).scalars()
        frame = pd.DataFrame([{"id": r.id, "symbol": r.symbol, "timestamp": r.timestamp, "interval": r.interval,
                               "action": r.action, "strategy": r.strategy, "confidence": r.confidence} for r in rows])
    finally:
        if own:
            session.close()
    if frame.empty:
        return frame
    stamp = pd.to_datetime(frame["timestamp"])
    frame["timestamp"] = stamp.dt.tz_localize("UTC") if stamp.dt.tz is None else stamp.dt.tz_convert("UTC")
    return frame


def price_window(candles: pd.DataFrame, signal_time: pd.Timestamp, horizon: int) -> dict:
    """Entry/exit prices and bar times for one signal, or a status explaining why there are none."""
    local = signal_time.tz_convert(IST)
    if local.hour * 60 + local.minute >= EXIT_MINUTE:
        return {"status": "after_cutoff"}
    if candles is None or candles.empty:
        return {"status": "unpriced"}
    stamp = pd.to_datetime(candles["timestamp"], utc=True)
    frame = candles.assign(stamp=stamp).sort_values("stamp")
    ist = frame["stamp"].dt.tz_convert(IST)
    frame = frame.assign(session=ist.dt.date, minute=ist.dt.hour * 60 + ist.dt.minute)
    after = frame[(frame["stamp"] >= signal_time) & (frame["session"] == local.date())]
    latest = frame["stamp"].max()
    if after.empty:
        return {"status": "pending" if latest < signal_time else "unpriced"}
    entry = after.iloc[0]
    cutoff = pd.Timestamp(f"{local.date()} 15:15", tz=IST).tz_convert("UTC")
    if entry["stamp"] >= cutoff:  # the next bar opens at or after the 15:15 exit: no trade to enter
        return {"status": "after_cutoff"}
    end = min(entry["stamp"] + pd.Timedelta(minutes=horizon), cutoff)
    if latest < end - pd.Timedelta(minutes=1):  # the store does not reach the end of the window yet
        return {"status": "pending"}
    exit_ = after[after["stamp"] < end].iloc[-1]
    return {"status": "scored", "entry_time": entry["stamp"], "exit_time": exit_["stamp"],
            "entry": float(entry["open"]), "exit": float(exit_["close"])}


def index_return(index: pd.DataFrame | None, entry_time, exit_time) -> float | None:
    if index is None or index.empty:
        return None
    stamp = pd.to_datetime(index["timestamp"], utc=True)
    start, finish = index[stamp == entry_time], index[stamp == exit_time]
    if start.empty or finish.empty:
        return None
    return float(finish["close"].iloc[0]) / float(start["open"].iloc[0]) - 1


def score(signals: pd.DataFrame, config: ScoreConfig = ScoreConfig(), store=None) -> pd.DataFrame:
    """One row per signal with status, net return and alpha."""
    from trading_agent.storage.parquet_store import get_parquet_store

    store = store or get_parquet_store()
    if signals.empty:
        return signals.assign(status=[], net=[], alpha=[])
    cost = config.cost_bps / 1e4
    out = []
    start = signals["timestamp"].min().to_pydatetime() - timedelta(days=1)
    for (symbol, interval), group in signals.groupby(["symbol", "interval"]):
        try:
            candles = store.read_candles(symbol, interval, start=start)
        except Exception:  # unreadable or missing data: unpriced, not a loss
            candles = None
        try:
            index = store.read_candles(INDEX, interval, start=start)
        except Exception:
            index = None
        for row in group.itertuples(index=False):
            base = {"id": row.id, "symbol": symbol, "strategy": row.strategy, "action": row.action,
                    "confidence": row.confidence, "timestamp": row.timestamp,
                    "session": row.timestamp.tz_convert(IST).date()}
            if row.action not in ("BUY", "SELL"):
                out.append({**base, "status": "hold", "net": np.nan, "alpha": np.nan})
                continue
            priced = price_window(candles, row.timestamp, config.horizon_minutes)
            if priced["status"] != "scored":
                out.append({**base, "status": priced["status"], "net": np.nan, "alpha": np.nan})
                continue
            sign = 1 if row.action == "BUY" else -1
            raw = priced["exit"] / priced["entry"] - 1
            bench = index_return(index, priced["entry_time"], priced["exit_time"])
            out.append({**base, "status": "scored", "net": sign * raw - cost,
                        "alpha": sign * (raw - bench) - cost if bench is not None else np.nan})
    return pd.DataFrame(out)


def _stats(rows: pd.DataFrame) -> dict:
    scored = rows[rows["status"] == "scored"]
    counts = rows["status"].value_counts().to_dict()
    out = {"signals": int(len(rows)), "scored": int(len(scored)),
           **{k: int(counts.get(k, 0)) for k in ("hold", "pending", "unpriced", "after_cutoff")}}
    if scored.empty:
        return out
    daily = scored.groupby("session")["net"].sum()
    std = float(daily.std(ddof=1)) if len(daily) > 1 else 0.0
    alpha = scored["alpha"].dropna()
    out.update(
        mean_net_bps=round(float(scored["net"].mean()) * 1e4, 2),
        win_rate_pct=round(float((scored["net"] > 0).mean()) * 100, 1),
        sessions=int(len(daily)),
        session_t=round(float(daily.mean() / (std / math.sqrt(len(daily)))), 2) if std > 0 else None,
        mean_alpha_bps=round(float(alpha.mean()) * 1e4, 2) if len(alpha) else None,
    )
    return out


def summarize(scored: pd.DataFrame) -> dict:
    """By strategy, by strategy and action, and by strategy and confidence band."""
    if scored.empty:
        return {"by_strategy": {}, "by_action": {}, "by_confidence": {}}
    band = pd.cut(scored["confidence"].fillna(0).clip(0, 1), [b[0] for b in CONFIDENCE_BANDS] + [1.01],
                  right=False, labels=[b[2] for b in CONFIDENCE_BANDS])
    rows = scored.assign(band=band.astype(str))
    return {
        "by_strategy": {s: _stats(g) for s, g in rows.groupby("strategy")},
        "by_action": {f"{s} {a}": _stats(g) for (s, a), g in rows.groupby(["strategy", "action"])},
        "by_confidence": {f"{s} {b}": _stats(g) for (s, b), g in rows[rows["action"].isin(["BUY", "SELL"])]
                          .groupby(["strategy", "band"])},
    }


def report_path():
    return get_settings().data_dir / "research" / "decision_outcomes" / "latest.json"


def run(config: ScoreConfig = ScoreConfig(), store=None, session=None, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    signals = load_signals(now - timedelta(days=config.days), session)
    scored = score(signals, config, store)
    llm_mock = get_settings().llm.provider == "mock"
    report = {
        "generated_at": now.isoformat(), "config": asdict(config), "scope": "research_only",
        "benchmark": INDEX, **summarize(scored),
        "notes": ("Entry at the first bar at or after the signal; exit after the horizon or at 15:15, net of costs. "
                  "Signals are scored independently, so overlapping signals are correlated; session_t uses "
                  "per-session sums. Pending, unpriced, after-cutoff and HOLD rows are not losses.")
                 + (" LLM_PROVIDER is 'mock': stored llm signals come from the mock client, not a model."
                    if llm_mock else ""),
    }
    path = report_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, default=str, indent=1), encoding="utf-8")
    tmp.replace(path)
    return report


def read_report() -> dict | None:
    try:
        return json.loads(report_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Score stored rule/ML/LLM decisions on realised returns")
    parser.add_argument("--days", type=int, default=ScoreConfig.days)
    parser.add_argument("--horizon", type=int, default=ScoreConfig.horizon_minutes, help="Minutes held")
    parser.add_argument("--cost-bps", type=float, default=ScoreConfig.cost_bps)
    args = parser.parse_args(argv)
    report = run(ScoreConfig(days=args.days, horizon_minutes=args.horizon, cost_bps=args.cost_bps))
    print(json.dumps({k: report[k] for k in ("by_strategy", "by_action", "by_confidence", "notes")},
                     indent=1, default=str))


if __name__ == "__main__":
    main()
