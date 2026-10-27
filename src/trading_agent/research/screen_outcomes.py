"""Do the screener's trading screens predict anything intraday? A historical check.

For every stored session, each screen is evaluated as it would have been at a
decision time (default 10:15 IST), using only what was known then:
previous-session floor pivots, RSI(14)/MACD(12,26,9) on completed sessions, the
52-week high from completed sessions, and volume so far against the same time
of day over the previous 20 sessions. Flagged stocks are entered at the open of
the 10:15 bar and exited at the close of the last bar before 15:15 (MIS
square-off), on the screen's side (bullish long, bearish short).

Two numbers per screen, per session, then averaged over sessions:

- net: the screen's side times the flagged names' mean return, minus ``cost_bps``.
- edge vs universe: the same side times (flagged mean - all liquid names' mean).
  A random pick earns the universe mean, so this is the random baseline.

Sessions are split in time: the first part is development, the last
``holdout_fraction`` the holdout. Ten screens are tested at once, so a screen
is an ``edge_candidate`` only with positive net and edge in both parts and a
holdout t-stat of at least 2.81 (Bonferroni for 10 tests, 5% two-sided).

Research only: no orders or sizing. Survivorship bias (delisted names are
missing) flatters results; previous closes are the last stored minute bar, not
the official close; "volume shockers" use time-of-day relative volume here,
not Groww's full-day average, which is meaningless at 10:15.
"""

from __future__ import annotations

import json
import math
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from trading_agent.analysis.screener import DAY_NS, IST_OFFSET_NS, NEAR_52W_PCT, NEAR_LEVEL_PCT, PRESETS, _utc
from trading_agent.core.config import get_settings
from trading_agent.core.logging_config import get_logger
from trading_agent.core.quant_indicators import macd, rsi

logger = get_logger(__name__)

SESSION_OPEN = 9 * 60 + 15
EXIT_MINUTE = 15 * 60 + 15
CRORE = 1e7
RVOL_SESSIONS = 20
BONFERRONI_T = 2.81
SIDE = {"bullish": 1, "bearish": -1, "neutral": 1}
SCREENS = tuple(PRESETS)
WORKERS = max(1, min(4, (os.cpu_count() or 2) - 1))


@dataclass(frozen=True)
class OutcomeConfig:
    decision_minute: int = 10 * 60 + 15  # IST minute of day
    cost_bps: float = 14.0  # round trip, as the other research modules
    min_avg_value_cr: float = 10.0  # liquidity floor: 20-session mean daily traded value
    holdout_fraction: float = 1 / 3
    history_days: int = 400


def symbol_sessions(symbol: str, frame: pd.DataFrame, decision_minute: int) -> pd.DataFrame:
    """One row per session: features known at ``decision_minute``, the trade's return and screen flags."""
    if frame is None or frame.empty:
        return pd.DataFrame()
    ts = _utc(frame["timestamp"])
    nanos = ts.dt.tz_convert("UTC").dt.tz_localize(None).to_numpy("datetime64[ns]").astype(np.int64) + IST_OFFSET_NS
    df = frame.assign(session=nanos // DAY_NS, minute=(nanos % DAY_NS) // (60 * 10**9)).sort_values("timestamp")
    df = df[(df["minute"] >= SESSION_OPEN) & (df["minute"] < 15 * 60 + 30)]
    if df.empty:
        return pd.DataFrame()
    day = df.groupby("session").agg(high=("high", "max"), low=("low", "min"), close=("close", "last"),
                                    volume=("volume", "sum"), first=("minute", "min"))
    before = df[df["minute"] < decision_minute].groupby("session").agg(
        price=("close", "last"), volume_so_far=("volume", "sum"))
    window = df[(df["minute"] >= decision_minute) & (df["minute"] < EXIT_MINUTE)].groupby("session").agg(
        entry=("open", "first"), exit=("close", "last"), exit_minute=("minute", "max"))
    d = day.join(before, how="left").join(window, how="left")

    closes = d["close"].to_numpy(dtype=float)
    prev = d[["high", "low", "close"]].shift(1)
    pivot = (prev["high"] + prev["low"] + prev["close"]) / 3
    d["r1"], d["s1"] = 2 * pivot - prev["low"], 2 * pivot - prev["high"]
    d["prev_close"] = prev["close"]
    d["rsi"] = pd.Series(rsi(closes, 14) if len(closes) > 14 else np.full(len(closes), np.nan), d.index).shift(1)
    line, signal, _ = macd(closes)
    d["macd"], d["macd_hist"] = pd.Series(line, d.index).shift(1), pd.Series(line - signal, d.index).shift(1)
    d.loc[d.index[:35], ["macd", "macd_hist"]] = np.nan  # EMA warm-up, as the live screener
    d["year_high"] = d["high"].rolling(250, min_periods=60).max().shift(1)
    d["rvol"] = d["volume_so_far"] / d["volume_so_far"].rolling(RVOL_SESSIONS, min_periods=5).mean().shift(1)
    d["avg_value_cr"] = (d["close"] * d["volume"]).rolling(20, min_periods=10).mean().shift(1) / CRORE

    # Complete sessions only: a 09:15 bar, a price before the decision, and an exit near 15:15.
    d = d[(d["first"] <= SESSION_OPEN + 5) & d["price"].gt(0) & d["entry"].gt(0) & d["exit"].gt(0)
          & (d["exit_minute"] >= EXIT_MINUTE - 15) & d["prev_close"].gt(0)].copy()
    if d.empty:
        return pd.DataFrame()
    p = d["price"]
    flags = {
        "resistance-breakout": p > d["r1"],
        "near-resistance": ((d["r1"] - p) / d["r1"] * 100).between(0, NEAR_LEVEL_PCT),
        "support-breakdown": p < d["s1"],
        "macd-positive": d["macd"] > 0,
        "macd-above-signal": d["macd_hist"] > 0,
        "macd-below-signal": d["macd_hist"] <= 0,
        "rsi-overbought": d["rsi"] >= 70,
        "rsi-oversold": d["rsi"] <= 30,
        "near-52w-high": ((d["year_high"] - p) / d["year_high"] * 100) <= NEAR_52W_PCT,
        "volume-shockers": d["rvol"] >= 2,
    }
    out = pd.DataFrame({"session": pd.to_datetime(d.index.to_numpy() * DAY_NS).date, "symbol": symbol,
                        "ret": d["exit"].to_numpy() / d["entry"].to_numpy() - 1,
                        "avg_value_cr": d["avg_value_cr"].to_numpy()})
    for name in SCREENS:
        out[name] = flags[name].fillna(False).to_numpy(dtype=bool)
    return out


def _symbol_task(task: tuple) -> pd.DataFrame:
    symbol, decision_minute, start = task
    from trading_agent.storage.parquet_store import get_parquet_store

    store = get_parquet_store()
    store.cache_rows = 0  # each symbol is read once
    try:
        frame = store.read_candles(symbol, "minute", start=start,
                                   columns=["timestamp", "open", "high", "low", "close", "volume"])
        return symbol_sessions(symbol, frame, decision_minute)
    except Exception as error:  # one bad symbol must not lose the run
        logger.warning("screen_outcomes_symbol_failed", symbol=symbol, error=type(error).__name__)
        return pd.DataFrame()


def build_panel(symbols: list[str], config: OutcomeConfig = OutcomeConfig()) -> pd.DataFrame:
    start = datetime.now(UTC) - timedelta(days=config.history_days)
    tasks = [(s, config.decision_minute, start) for s in symbols]
    with ProcessPoolExecutor(max_workers=WORKERS, mp_context=multiprocessing.get_context("spawn")) as pool:
        frames = [f for f in pool.map(_symbol_task, tasks, chunksize=16) if not f.empty]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _stats(daily: np.ndarray) -> dict:
    if not len(daily):
        return {"sessions": 0}
    std = float(daily.std(ddof=1)) if len(daily) > 1 else 0.0
    return {"sessions": int(len(daily)), "mean_bps": round(float(daily.mean()) * 1e4, 2),
            "hit_rate_pct": round(float((daily > 0).mean()) * 100, 1),
            "t_stat": round(float(daily.mean() / (std / math.sqrt(len(daily)))), 2) if std > 0 else None}


def summarize(panel: pd.DataFrame, config: OutcomeConfig = OutcomeConfig()) -> dict:
    """Per screen: development and holdout net and edge vs the liquid universe, with a verdict."""
    liquid = panel[panel["avg_value_cr"] >= config.min_avg_value_cr]
    sessions = sorted(liquid["session"].unique())
    if len(sessions) < 30:
        return {"status": "insufficient_history", "sessions": len(sessions), "required_sessions": 30}
    cut = sessions[int(len(sessions) * (1 - config.holdout_fraction))]
    universe = liquid.groupby("session")["ret"].mean()
    cost = config.cost_bps / 1e4
    screens = []
    for name in SCREENS:
        side = SIDE[PRESETS[name]["bias"]]
        picked = liquid[liquid[name]]
        by_day = picked.groupby("session")["ret"].agg(["mean", "size"])
        net = side * by_day["mean"] - cost
        edge = side * (by_day["mean"] - universe.reindex(by_day.index))
        parts = {}
        for part, mask in (("development", by_day.index < cut), ("holdout", by_day.index >= cut)):
            parts[part] = {"net": _stats(net[mask].to_numpy()), "edge_vs_universe": _stats(edge[mask].to_numpy()),
                           "avg_names": round(float(by_day["size"][mask].mean()), 1) if mask.any() else 0}
        dev, hold = parts["development"], parts["holdout"]
        positive = all(p[k].get("mean_bps", 0) > 0 for p in (dev, hold) for k in ("net", "edge_vs_universe"))
        if hold["net"]["sessions"] < 20:
            verdict = "insufficient_sessions"
        elif positive and (hold["net"].get("t_stat") or 0) >= BONFERRONI_T:
            verdict = "edge_candidate"
        elif hold["net"].get("mean_bps", 0) <= 0:
            verdict = "no_edge"
        else:
            verdict = "inconclusive"
        screens.append({"id": name, "title": PRESETS[name]["title"], "side": "long" if side > 0 else "short",
                        "verdict": verdict, **parts})
    baseline = {"long_net": _stats(universe.to_numpy() - cost), "short_net": _stats(-universe.to_numpy() - cost)}
    return {"status": "completed", "sessions": len(sessions), "first_session": str(sessions[0]),
            "holdout_from": str(cut), "last_session": str(sessions[-1]),
            "symbols": int(liquid["symbol"].nunique()), "universe_baseline": baseline,
            "screens": screens}


def report_path():
    return get_settings().data_dir / "research" / "screen_outcomes.json"


def read_report() -> dict | None:
    try:
        return json.loads(report_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def run(symbols: list[str] | None = None, config: OutcomeConfig = OutcomeConfig()) -> dict:
    from trading_agent.research.cross_sectional import default_universe

    symbols = symbols or default_universe()
    panel = build_panel(symbols, config)
    report = summarize(panel, config) if not panel.empty else {"status": "no_data", "sessions": 0}
    report.update(generated_at=datetime.now(UTC).isoformat(), config=asdict(config),
                  symbols_requested=len(symbols), method=__doc__.split("\n\n")[1].replace("\n", " "),
                  limitations=("Research only. Survivorship bias; previous closes from stored bars; entry at the "
                               "10:15 bar's open and exit at the last bar before 15:15 are assumptions; "
                               "cost_bps must cover charges and slippage."))
    path = report_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, allow_nan=False), encoding="utf-8")
    temporary.replace(path)
    return report
