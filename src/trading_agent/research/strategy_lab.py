"""Bounded, exploratory comparisons using the shared conservative bracket replay.

Results describe equal-notional trades, not a compounded portfolio. No model is
trained, no evidence is promoted, and historical loops never read the store.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import itertools
import math

import numpy as np
import pandas as pd

from trading_agent.core.position_sizing import PositionSizingConfig
from trading_agent.core.signals import SignalParams, evaluate_signals
from trading_agent.research.trade_outcomes import TradePolicy, replay
from trading_agent.storage.parquet_store import ParquetStore, get_parquet_store

SECONDS = {"minute": 60, "5minute": 300, "15minute": 900}
MAX_BARS = 10000
MIN_BARS = 250
MAX_SEARCH_TRIALS = 256
PROFILES = (
    ("rule", "Confirmed patterns", "Existing RSI/ADX rules with confirmed candle/swing patterns."),
    ("trend", "Trend pullback", "MA20/MA50 trend, ADX >= 20 and RSI pullback."),
    ("breakout", "20-bar breakout", "Close beyond the prior 20-bar range with ADX >= 20."),
)
LIMITATIONS = (
    "Exploratory comparisons only; repeated inspection can overfit the evaluation window. "
    "No profile is qualified or promoted. Returns sum equal-notional trade returns; they "
    "are not portfolio growth. Costs are assumptions. Replay uses next-bar open entries, "
    "adverse slippage, stop-first ambiguous bars, no overlapping positions, and bounded "
    "intraday exits. Missing outcome bars are excluded. No order-book fills, corporate-action "
    "adjustment or official holiday calendar. Independent prospective evidence is still required."
)


@dataclass(frozen=True)
class LabConfig:
    """Compatibility settings mapped explicitly to one shared execution policy."""

    forward_window: int = 24
    slippage_pct: float = 0.0002
    commission: float = 0.0
    cost_bps: float = 10.0
    stop_atr: float = 1.5
    reward_risk: float = 2.0

    def policy(self) -> TradePolicy:
        if self.commission != 0:
            raise ValueError("Fixed cash commission needs position sizing; use round-trip cost_bps")
        return TradePolicy(horizon=self.forward_window, slippage_bps=self.slippage_pct * 10000,
                           cost_bps=self.cost_bps, stop_atr=self.stop_atr,
                           reward_risk=self.reward_risk)


DEFAULT_LAB_CONFIG = LabConfig()


def _validate_interval(interval: str) -> int:
    if interval not in SECONDS:
        raise ValueError("Strategy lab supports minute, 5minute and 15minute intervals")
    return SECONDS[interval]


def prepare_frame(raw: pd.DataFrame) -> pd.DataFrame:
    """Validate chronological OHLCV, then compute only causally available features."""
    from trading_agent.strategy.research_features import prepare_research_frame

    columns = ["timestamp", "open", "high", "low", "close", "volume"]
    if not set(columns).issubset(raw.columns):
        raise ValueError("OHLCV history must contain timestamp, open, high, low, close and volume")
    if len(raw) > MAX_BARS:
        raise ValueError(f"Strategy lab accepts at most {MAX_BARS} candles")
    frame = raw[columns].copy().reset_index(drop=True)
    try:
        frame["timestamp"] = pd.to_datetime(frame.timestamp, utc=True, format="mixed")
        frame[columns[1:]] = frame[columns[1:]].apply(pd.to_numeric, errors="raise")
        values = frame[columns[1:]].to_numpy(dtype=float)
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid timestamp or OHLCV values") from error
    if (frame.timestamp.isna().any() or frame.timestamp.duplicated().any()
            or not frame.timestamp.is_monotonic_increasing):
        raise ValueError("OHLCV timestamps must be valid, unique and chronological")
    if (not np.isfinite(values).all() or (values[:, :4] <= 0).any()
            or (values[:, 4] < 0).any()
            or (frame.low > frame[["open", "close"]].min(axis=1)).any()
            or (frame.high < frame[["open", "close"]].max(axis=1)).any()):
        raise ValueError("Invalid OHLCV prices, ranges or volume")
    return prepare_research_frame(frame)


def profile_actions(frame: pd.DataFrame, profile: str = "rule",
                    params: SignalParams | None = None) -> np.ndarray:
    """Fixed profiles use current/past candles; swings require confirmation first."""
    if profile == "rule":
        selected = params or SignalParams()
        if not np.isfinite(list(asdict(selected).values())).all():
            raise ValueError("Signal parameters must be finite")
        return evaluate_signals(frame.rsi.to_numpy(), frame.adx.to_numpy(),
                                frame.pattern_code.to_numpy(), params=selected)
    actions = np.zeros(len(frame), dtype=np.int8)
    if profile == "trend":
        buy = (frame.ma20 > frame.ma50) & frame.rsi.between(40, 50)
        sell = (frame.ma20 < frame.ma50) & frame.rsi.between(50, 60)
    elif profile == "breakout":
        buy = frame.close > frame.high.shift(1).rolling(20).max()
        sell = frame.close < frame.low.shift(1).rolling(20).min()
    else:
        raise ValueError(f"Unknown strategy profile: {profile}")
    actions[(buy & (frame.adx >= 20)).to_numpy()] = 1
    actions[(sell & (frame.adx >= 20)).to_numpy()] = 2
    return actions


def summarize(trades: list[dict]) -> dict:
    """Finite JSON metrics; unavailable statistics remain null, never invented."""
    result = {"trades": len(trades), "sessions": len({t["session"] for t in trades}),
              "win_rate_pct": None, "mean_net_return_pct": None,
              "total_net_return_pct": None, "max_drawdown_pct_points": None,
              "profit_factor": None}
    if not trades:
        return result
    returns = np.array([t["net_return"] for t in trades], dtype=float)
    if not np.isfinite(returns).all():
        raise ValueError("Replay produced non-finite returns")
    curve = np.r_[0.0, returns.cumsum()]
    losses = -returns[returns < 0].sum()
    result.update(win_rate_pct=float((returns > 0).mean() * 100),
                  mean_net_return_pct=float(returns.mean() * 100),
                  total_net_return_pct=float(returns.sum() * 100),
                  max_drawdown_pct_points=float((np.maximum.accumulate(curve) - curve).max() * 100),
                  profit_factor=float(returns[returns > 0].sum() / losses) if losses > 0 else None)
    return result


def compare_profiles(raw: pd.DataFrame, symbol: str, interval: str = "minute",
                     policy: TradePolicy | None = None) -> dict:
    """Compare predefined profiles on the same chronological 60/40 partitions.

    The reference partition is descriptive, not fitted. Both partitions own
    their complete outcome bars; the replay excludes horizon-spanning entries.
    """
    seconds = _validate_interval(interval)
    symbol = symbol.strip().upper()
    ParquetStore._validate_key(symbol)
    policy = policy or TradePolicy()
    result = {"version": "strategy-lab-v1", "status": "insufficient_history",
              "scope": "exploratory_only", "promotion_eligible": False,
              "symbol": symbol, "interval": interval, "candles": len(raw),
              "required_candles": max(MIN_BARS, math.ceil((policy.horizon + 61) / .4)),
              "policy": asdict(policy), "windows": None, "profiles": [], "baselines": [],
              "limitations": LIMITATIONS}
    if raw.empty:
        return result
    frame = prepare_frame(raw)
    if len(frame) < result["required_candles"]:
        return result
    split = int(len(frame) * .6)
    result.update(status="completed", windows={
        "reference_start": frame.timestamp.iloc[0].isoformat(),
        "reference_end": frame.timestamp.iloc[split - 1].isoformat(),
        "evaluation_start": frame.timestamp.iloc[split].isoformat(),
        "evaluation_end": frame.timestamp.iloc[-1].isoformat(),
    })
    scores = np.zeros(len(frame))  # No probabilities are inferred from rule actions.

    def measure(identifier: str, label: str, description: str, actions: np.ndarray) -> dict:
        reference = replay(frame, actions, scores, 0, split, policy, seconds)
        evaluation = replay(frame, actions, scores, split, len(frame), policy, seconds)
        return {"id": identifier, "label": label, "description": description,
                "metrics": summarize(evaluation), "reference": summarize(reference),
                "sides": {side: summarize([t for t in evaluation if t["side"] == side])
                          for side in ("BUY", "SELL")}}

    for identifier, label, description in PROFILES:
        result["profiles"].append(measure(identifier, label, description,
                                          profile_actions(frame, profile=identifier)))
    for code, side in ((1, "long"), (2, "short")):
        result["baselines"].append(measure(f"always_{side}", f"Unconditional {side}",
                                           "Enter at every eligible opportunity under the same policy.",
                                           np.full(len(frame), code, dtype=np.int8)))
    return result


def read_completed(symbol: str, interval: str = "minute", max_bars: int = 5000) -> pd.DataFrame:
    """Read local candles once; exclude the current incomplete or future candle."""
    seconds = _validate_interval(interval)
    ParquetStore._validate_key(symbol)
    if not isinstance(max_bars, int) or not MIN_BARS <= max_bars <= MAX_BARS:
        raise ValueError(f"max_bars must be {MIN_BARS}-{MAX_BARS}")
    now = pd.Timestamp(datetime.now(UTC))
    frame = get_parquet_store().read_latest(symbol, interval, n=max_bars + 1)
    if frame.empty:
        return frame
    frame = frame.copy()
    frame["timestamp"] = pd.to_datetime(frame.timestamp, utc=True, format="mixed")
    return frame.loc[frame.timestamp + pd.Timedelta(seconds=seconds) <= now].tail(max_bars).reset_index(drop=True)


@dataclass
class StrategyResult:
    """Legacy result names; returns/DD describe additive equal-notional trades."""

    params: SignalParams
    total_trades: int
    win_rate: float | None
    gross_ev: float | None
    net_ev: float | None
    total_return: float | None
    max_drawdown: float | None
    sharpe: float | None
    profit_factor: float | None


class StrategyLab:
    """Compatibility entry points using the shared bracket execution contract."""

    def __init__(self, config: LabConfig = DEFAULT_LAB_CONFIG, interval: str = "minute"):
        self.config = config
        self.policy = config.policy()
        self.seconds = _validate_interval(interval)

    def test_params(self, df: pd.DataFrame, params: SignalParams,
                    position_config: PositionSizingConfig | None = None) -> StrategyResult:
        if position_config is not None:
            raise ValueError("Strategy lab reports equal-notional returns; portfolio sizing is unsupported")
        frame = prepare_frame(df)
        actions = profile_actions(frame, params=params)
        trades = replay(frame, actions, np.zeros(len(frame)), 0, len(frame), self.policy, self.seconds)
        metrics = summarize(trades)
        return StrategyResult(params, metrics["trades"], metrics["win_rate_pct"], None,
                              metrics["mean_net_return_pct"], metrics["total_net_return_pct"],
                              metrics["max_drawdown_pct_points"], None, metrics["profit_factor"])

    def grid_search(self, df: pd.DataFrame, param_grid: dict[str, list]) -> list[StrategyResult]:
        trials = math.prod(len(values) for values in param_grid.values())
        if not 1 <= trials <= MAX_SEARCH_TRIALS:
            raise ValueError(f"Search must contain 1-{MAX_SEARCH_TRIALS} trials")
        results = [self.test_params(df, SignalParams(**dict(zip(param_grid, values, strict=True))))
                   for values in itertools.product(*param_grid.values())]
        return sorted(results, key=lambda result: result.net_ev if result.net_ev is not None else -np.inf,
                      reverse=True)

    def random_search(self, df: pd.DataFrame, param_ranges: dict[str, tuple], n_iter: int = 100,
                      seed: int = 0) -> list[StrategyResult]:
        if not isinstance(n_iter, int) or not 1 <= n_iter <= MAX_SEARCH_TRIALS:
            raise ValueError(f"Search must contain 1-{MAX_SEARCH_TRIALS} trials")
        rng = np.random.default_rng(seed)
        results = []
        for _ in range(n_iter):
            params = {key: rng.integers(low, high + 1) if isinstance(low, int) else rng.uniform(low, high)
                      for key, (low, high) in param_ranges.items()}
            results.append(self.test_params(df, SignalParams(**params)))
        return sorted(results, key=lambda result: result.net_ev if result.net_ev is not None else -np.inf,
                      reverse=True)


def run_strategy_lab(symbol: str, interval: str = "5minute", param_grid: dict | None = None,
                     **kwargs) -> list[StrategyResult]:
    """Compatibility helper; an explicit grid opts into exploratory parameter search."""
    frame = read_completed(symbol.strip().upper(), interval)
    if frame.empty:
        raise ValueError(f"No completed data for {symbol}")
    lab = StrategyLab(interval=interval, **kwargs)
    return lab.grid_search(frame, param_grid) if param_grid is not None else [lab.test_params(frame, SignalParams())]
