"""Cross-sectional intraday ranking (adapted from Ghosh, Neufeld & Sahoo, 2021).

Each session, a random forest ranks the universe by the probability that a
stock's open-to-close return beats that day's cross-sectional median. The
strategy buys the top ``k`` at the open, shorts the bottom ``k``, and closes all
of them at the close, so it is market-neutral by construction.

Features use only information known at the open of day t ("close" is the
15:15 price when exits are used; see ``build_features``):
  IntraR_k  open-to-close return of day t-k
  CloseR_k  close-to-close return from t-1-k to t-1
  OverNR_k  return from the close of t-k to the open of t

The paper uses lags 1-20 plus 40-240 in steps of 20 on S&P 500 constituents;
``max_lag`` trims that list to the history actually stored. The walk-forward
retrains every ``retrain_days`` on the preceding ``train_days`` sessions, and
each test block is strictly after its training data.

This is research. Nothing here places orders, sizes positions or promotes a
model. The research gate is a minimum bar, not proof of an edge: today's
watchlist has survivorship bias (delisted names are missing), and entering
exactly at the open is optimistic, so ``cost_bps`` should include slippage.
"""

from __future__ import annotations

import json
import math
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from trading_agent.core.config import get_settings
from trading_agent.core.logging_config import get_logger

logger = get_logger(__name__)

IST = "Asia/Kolkata"
PAPER_LAGS = tuple(range(1, 21)) + tuple(range(40, 241, 20))
SESSION_OPEN, LAST_BAR = 9 * 60 + 15, 15 * 60 + 25
# MIS positions are squared off around 15:15-15:20, so the strategy exits at the
# close of the last bar that starts before 15:15, not at the 15:30 close.
EXIT_MINUTE = 15 * 60 + 15
CRORE = 1e7
LIMITATIONS = (
    "Research only; no orders, sizing or promotion. Today's watchlist omits delisted names "
    "(survivorship bias), which flatters backtests. Entry exactly at the open and exit at the "
    "close are assumptions; cost_bps must cover charges and slippage on both legs. "
    "Daily bars come from stored candles; sessions without a 09:15 bar or a 15:15 price are "
    "excluded. Passing the research gate is necessary, not sufficient: prospective paper "
    "results are still required."
)


@dataclass(frozen=True)
class RankConfig:
    # Defaults chosen on the validation half of a 9-configuration walk-forward
    # (2026-10-24, 15:15 exit, >= INR 10 crore liquidity, 15 bps): see MEMORY.md.
    max_lag: int = 10
    train_days: int = 60
    retrain_days: int = 20
    top_k: int = 10
    cost_bps: float = 15.0  # round trip per position, incl. slippage
    min_avg_value_cr: float = 10.0  # rank only stocks trading >= this many INR crore a day (20-day mean)
    n_estimators: int = 300
    max_depth: int = 10
    min_test_days: int = 60
    random_draws: int = 200
    seed: int = 9

    def __post_init__(self):
        if not 1 <= self.max_lag <= 240 or self.train_days < 20 or self.retrain_days < 1 or self.top_k < 1:
            raise ValueError("Invalid ranking configuration")
        if not 0 <= self.cost_bps <= 200:
            raise ValueError("cost_bps must be 0-200")

    @property
    def lags(self) -> tuple[int, ...]:
        return tuple(k for k in PAPER_LAGS if k <= self.max_lag)


# --- data -----------------------------------------------------------------------------

def daily_bars(intraday: pd.DataFrame) -> pd.DataFrame:
    """IST sessions -> open (09:15 bar), close (bar from 15:25 on), 15:15 exit price and traded value."""
    if intraday is None or intraday.empty:
        return pd.DataFrame(columns=["open", "close", "exit", "value"])
    stamp = pd.to_datetime(intraday["timestamp"], utc=True).dt.tz_convert(IST)
    frame = intraday.assign(session=stamp.dt.date, minute=stamp.dt.hour * 60 + stamp.dt.minute)
    frame = frame.sort_values("timestamp")
    grouped = frame.groupby("session")
    bars = pd.DataFrame({"open": grouped["open"].first(), "close": grouped["close"].last(),
                         "first": grouped["minute"].min(), "last": grouped["minute"].max()})
    bars["open"] = bars["open"].where(bars["first"] == SESSION_OPEN)
    bars["close"] = bars["close"].where(bars["last"] >= LAST_BAR)
    before_exit = frame[frame["minute"] < EXIT_MINUTE].groupby("session")
    bars["exit"] = before_exit["close"].last().where(before_exit["minute"].max() >= EXIT_MINUTE - 5)
    value = (frame["close"] * frame["volume"]).groupby(frame["session"]).sum()
    bars["value"] = value.where(value > 0)
    return bars[["open", "close", "exit", "value"]].dropna(how="all", subset=["open", "close"])


def load_panel(symbols: list[str], interval: str = "5minute", store=None,
               start: datetime | None = None) -> tuple[pd.DataFrame, ...]:
    """Wide open, close, 15:15 exit and traded-value frames (session x symbol) from stored candles."""
    from trading_agent.storage.parquet_store import get_parquet_store

    store = store or get_parquet_store()
    window = {"start": start} if start is not None else {}
    opens, closes, exits, values = {}, {}, {}, {}
    for symbol in symbols:
        try:
            bars = daily_bars(store.read_candles(symbol, interval, **window))
        except Exception as error:  # one unreadable symbol must not stop the panel
            logger.warning("cross_sectional_symbol_skipped", symbol=symbol, error=str(error))
            continue
        if len(bars):
            opens[symbol], closes[symbol] = bars["open"], bars["close"]
            exits[symbol], values[symbol] = bars["exit"], bars["value"]
    return tuple(pd.DataFrame(d).sort_index() for d in (opens, closes, exits, values))


def build_features(opens: pd.DataFrame, closes: pd.DataFrame, lags: tuple[int, ...],
                   exits: pd.DataFrame | None = None, values: pd.DataFrame | None = None,
                   min_avg_value: float = 0.0) -> pd.DataFrame:
    """Long table: one row per (session, symbol) with features, traded return and label.

    With ``exits``, every price in the features and the traded return (``ret``) is
    the 15:15 exit price, not the session close: since NSE's closing auction
    (2026-08-03) about 40% of the universe has no continuous-trading bars after
    15:14, so their session close is missing from the candles, and one price
    definition keeps stocks comparable before and after the change. Without
    ``exits`` the session close is used, as in the paper. With ``values``,
    a stock is eligible on a session only if its trailing 20-session mean traded
    value, known before that open, reaches ``min_avg_value``.
    """
    opens, closes = opens.align(closes, join="outer")
    if exits is not None:
        closes = exits.reindex_like(opens)
    intraday = closes / opens - 1
    traded = intraday
    parts = {}
    for k in lags:
        parts[f"IntraR{k}"] = intraday.shift(k)
        parts[f"CloseR{k}"] = closes.pct_change(k, fill_method=None).shift(1)
        parts[f"OverNR{k}"] = opens / closes.shift(k) - 1
    long = pd.concat({name: frame.stack(future_stack=True) for name, frame in parts.items()}, axis=1)
    long["open"] = opens.stack(future_stack=True)
    long["ret"] = traded.stack(future_stack=True)
    if values is not None and min_avg_value > 0:
        liquid = values.reindex_like(opens).rolling(20, min_periods=10).mean().shift(1) >= min_avg_value
        long["liquid"] = liquid.stack(future_stack=True)
    long.index.names = ["session", "symbol"]
    features = [c for c in long.columns if c[:6] in ("IntraR", "CloseR", "OverNR")]
    long = long.dropna(subset=features + ["open"])
    if "liquid" in long:
        long = long[long.pop("liquid").fillna(False).astype(bool)]
    # Label: beats the session's cross-sectional median (only where the outcome is known).
    median = long.groupby(level="session")["ret"].transform("median")
    long["label"] = (long["ret"] > median).astype(float).where(long["ret"].notna())
    return long


def feature_columns(table: pd.DataFrame) -> list[str]:
    return [c for c in table.columns if c[:6] in ("IntraR", "CloseR", "OverNR")]


# --- walk-forward ------------------------------------------------------------------------

def _model(config: RankConfig):
    from sklearn.ensemble import RandomForestClassifier

    return RandomForestClassifier(n_estimators=config.n_estimators, max_depth=config.max_depth,
                                  min_samples_leaf=5, random_state=config.seed, n_jobs=-1)


def _book(day: pd.DataFrame, scores: np.ndarray, k: int) -> tuple[float, list[str], list[str]]:
    order = np.argsort(scores)
    longs, shorts = order[-k:], order[:k]
    ret = day["ret"].to_numpy()
    gross = 0.5 * ret[longs].mean() - 0.5 * ret[shorts].mean()
    symbols = day.index.get_level_values("symbol")
    return float(gross), list(symbols[longs][::-1]), list(symbols[shorts])


def _stats(daily: np.ndarray) -> dict:
    if not len(daily):
        return {"days": 0}
    curve = np.r_[0.0, daily.cumsum()]
    std = float(daily.std(ddof=1)) if len(daily) > 1 else 0.0
    return {
        "days": int(len(daily)),
        "mean_daily_net_bps": round(float(daily.mean()) * 1e4, 3),
        "hit_rate_pct": round(float((daily > 0).mean()) * 100, 2),
        "t_stat": round(float(daily.mean() / (std / math.sqrt(len(daily)))), 3) if std > 0 else None,
        "annualised_sharpe": round(float(daily.mean() / std * math.sqrt(252)), 3) if std > 0 else None,
        "total_net_pct_points": round(float(daily.sum()) * 100, 3),
        "max_drawdown_pct_points": round(float((np.maximum.accumulate(curve) - curve).max()) * 100, 3),
    }


def walk_forward(table: pd.DataFrame, config: RankConfig) -> dict:
    """Rolling retrain; returns daily net returns of the model book and a random-pick baseline."""
    features = feature_columns(table)
    labelled = table.dropna(subset=["label"])
    sessions = sorted(labelled.index.get_level_values("session").unique())
    rng = np.random.default_rng(config.seed)
    cost = config.cost_bps / 1e4
    model_days, random_days, accuracy, blocks = [], [], [], []
    start = config.train_days
    while start < len(sessions):
        train_sessions = sessions[start - config.train_days:start]
        test_sessions = sessions[start:start + config.retrain_days]
        train = labelled.loc[labelled.index.get_level_values("session").isin(train_sessions)]
        if train["label"].nunique() < 2:
            start += config.retrain_days
            continue
        model = _model(config).fit(train[features].to_numpy(), train["label"].to_numpy())
        blocks.append({"train_start": str(train_sessions[0]), "train_end": str(train_sessions[-1]),
                       "test_start": str(test_sessions[0]), "test_end": str(test_sessions[-1])})
        for session in test_sessions:
            day = labelled.xs(session, level="session", drop_level=False)
            if len(day) < 2 * config.top_k:
                continue
            scores = model.predict_proba(day[features].to_numpy())[:, 1]
            accuracy.append(float(((scores > 0.5) == day["label"].to_numpy().astype(bool)).mean()))
            gross, _, _ = _book(day, scores, config.top_k)
            model_days.append((session, gross - cost))
            draws = [_book(day, rng.random(len(day)), config.top_k)[0] for _ in range(config.random_draws)]
            random_days.append(float(np.mean(draws)) - cost)
        start += config.retrain_days
    return {"model": model_days, "random": np.array(random_days), "accuracy": accuracy, "blocks": blocks}


def latest_ranking(table: pd.DataFrame, config: RankConfig, session=None) -> dict | None:
    """Rank ``session`` (default: the most recent with an open) on the preceding ``train_days`` labelled sessions."""
    features = feature_columns(table)
    sessions = sorted(table.index.get_level_values("session").unique())
    if not sessions or (session is not None and session not in sessions):
        return None
    target = session if session is not None else sessions[-1]
    labelled = table.dropna(subset=["label"])
    history = sorted(s for s in labelled.index.get_level_values("session").unique() if s < target)
    history = history[-config.train_days:]
    train = labelled.loc[labelled.index.get_level_values("session").isin(history)]
    day = table.xs(target, level="session", drop_level=False)
    if len(history) < config.train_days or train["label"].nunique() < 2 or len(day) < 2 * config.top_k:
        return None
    model = _model(config).fit(train[features].to_numpy(), train["label"].to_numpy())
    scores = model.predict_proba(day[features].to_numpy())[:, 1]
    order = np.argsort(scores)
    symbols = day.index.get_level_values("symbol")

    def pick(indices):
        return [{"symbol": symbols[i], "probability": round(float(scores[i]), 4)} for i in indices]

    return {"session": str(target), "long": pick(order[-config.top_k:][::-1]), "short": pick(order[:config.top_k]),
            "train_start": str(history[0]), "train_end": str(history[-1]), "eligible": sorted(symbols)}


def evaluate(opens: pd.DataFrame, closes: pd.DataFrame, config: RankConfig = RankConfig(),
             symbols_requested: int | None = None, interval: str = "5minute",
             exits: pd.DataFrame | None = None, values: pd.DataFrame | None = None) -> dict:
    """Full research report: walk-forward metrics, random baseline, gate, and (if passed) today's ranks."""
    report = {
        "version": "cross-sectional-v1", "scope": "research_only", "promotion_eligible": False,
        "generated_at": datetime.now(UTC).isoformat(), "interval": interval, "config": asdict(config),
        "lags": list(config.lags), "symbols_requested": symbols_requested,
        "symbols_with_data": int(opens.shape[1]), "sessions_with_data": int(len(opens.index)),
        "status": "insufficient_history", "limitations": LIMITATIONS,
    }
    needed = max(config.lags) + 1 + config.train_days + config.min_test_days
    report["required_sessions"] = needed
    if opens.shape[1] < 2 * config.top_k:
        report["reason"] = f"Need at least {2 * config.top_k} symbols with daily bars"
        return report
    table = build_features(opens, closes, config.lags, exits, values, config.min_avg_value_cr * CRORE)
    report["exit"] = "15:15 IST" if exits is not None else "session close"
    report["liquidity_filter_cr"] = config.min_avg_value_cr if values is not None else None
    labelled_sessions = table.dropna(subset=["label"]).index.get_level_values("session").nunique()
    report["labelled_sessions"] = int(labelled_sessions)
    if labelled_sessions < config.train_days + config.min_test_days:
        report["reason"] = (f"{labelled_sessions} usable sessions after the {max(config.lags)}-day lag window; "
                            f"need {config.train_days + config.min_test_days} (train {config.train_days} + "
                            f"test {config.min_test_days}). Backfill more history or lower --max-lag/--train-days.")
        return report
    result = walk_forward(table, config)
    model_daily = np.array([r for _, r in result["model"]])
    report.update(status="completed", blocks=len(result["blocks"]), windows=result["blocks"][:3] + result["blocks"][-1:],
                  model=_stats(model_daily), random_baseline=_stats(result["random"]),
                  classifier_accuracy_pct=round(float(np.mean(result["accuracy"])) * 100, 2) if result["accuracy"] else None,
                  daily=[{"session": str(s), "net": round(r, 6)} for s, r in result["model"]][-250:])
    m, b = report["model"], report["random_baseline"]
    checks = {
        "enough_test_days": m["days"] >= config.min_test_days,
        "positive_after_costs": m.get("mean_daily_net_bps", 0) > 0,
        "t_stat_at_least_2": (m.get("t_stat") or 0) >= 2,
        "beats_random_baseline": m.get("mean_daily_net_bps", 0) > b.get("mean_daily_net_bps", 0),
    }
    report["research_gate"] = {"passed": all(checks.values()), "checks": checks}
    if report["research_gate"]["passed"]:
        report["latest_ranking"] = latest_ranking(table, config)
    else:
        report["latest_ranking"] = None
        report["ranking_withheld"] = "Research gate not passed; no long/short list is published"
    return report


# --- persistence and background job ---------------------------------------------------------

def report_path():
    return get_settings().data_dir / "research" / "cross_sectional" / "latest.json"


def read_report() -> dict | None:
    path = report_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_report(report: dict) -> None:
    path = report_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, default=str), encoding="utf-8")
    tmp.replace(path)


def default_universe() -> list[str]:
    from trading_agent.paths import PROJECT_ROOT

    path = PROJECT_ROOT / "symbols_nifty500_plus.txt"
    if path.exists():
        return sorted({s.strip().upper() for s in path.read_text(encoding="utf-8-sig").splitlines()
                       if s.strip() and not s.startswith("#")})
    from trading_agent.analysis.research_engine import universe

    return universe()


def run(symbols: list[str] | None = None, interval: str = "5minute", config: RankConfig = RankConfig()) -> dict:
    symbols = symbols or default_universe()
    opens, closes, exits, values = load_panel(symbols, interval)
    report = evaluate(opens, closes, config, symbols_requested=len(symbols), interval=interval,
                      exits=exits, values=values)
    write_report(report)
    return report


class RankingJob:
    """One background evaluation at a time per API process."""

    def __init__(self):
        self._lock = threading.Lock()
        self.state = {"running": False, "started_at": None, "error": None}

    def start(self, symbols=None, interval="5minute", config: RankConfig = RankConfig()) -> bool:
        if not self._lock.acquire(blocking=False):
            return False
        self.state = {"running": True, "started_at": datetime.now(UTC).isoformat(), "error": None}

        def work():
            try:
                run(symbols, interval, config)
            except Exception as error:  # surfaced through status, never silently dropped
                logger.exception("cross_sectional_failed")
                self.state["error"] = str(error)
            finally:
                self.state["running"] = False
                self._lock.release()

        threading.Thread(target=work, name="cross-sectional", daemon=True).start()
        return True


_job = RankingJob()


def get_job() -> RankingJob:
    return _job


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Cross-sectional intraday ranking walk-forward (research only)")
    parser.add_argument("symbols", nargs="*", help="Default: symbols_nifty500_plus.txt")
    parser.add_argument("--interval", choices=["minute", "5minute", "15minute"], default="5minute")
    parser.add_argument("--max-lag", type=int, default=RankConfig.max_lag, help="Paper: 240")
    parser.add_argument("--train-days", type=int, default=RankConfig.train_days)
    parser.add_argument("--retrain-days", type=int, default=RankConfig.retrain_days)
    parser.add_argument("--top-k", type=int, default=RankConfig.top_k)
    parser.add_argument("--cost-bps", type=float, default=RankConfig.cost_bps)
    parser.add_argument("--min-value-cr", type=float, default=RankConfig.min_avg_value_cr,
                        help="Minimum 20-day mean traded value in INR crore")
    args = parser.parse_args(argv)
    config = RankConfig(max_lag=args.max_lag, train_days=args.train_days, retrain_days=args.retrain_days,
                        top_k=args.top_k, cost_bps=args.cost_bps, min_avg_value_cr=args.min_value_cr)
    report = run([s.upper() for s in args.symbols] or None, args.interval, config)
    print(json.dumps({k: v for k, v in report.items() if k not in ("daily", "limitations")}, indent=2, default=str))
    print(report["limitations"])
    if report["status"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
