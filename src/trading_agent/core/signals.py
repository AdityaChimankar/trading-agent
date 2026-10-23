"""Signal evaluation - Rule-based signal generation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from trading_agent.core.pattern_detection import compute_pattern_bias
from trading_agent.core.quant_indicators import prepare_indicators


# Constants used by research modules
FORWARD_WINDOW = 12
LOOKBACK_MIN = 30
SWING_ORDER = 5
MAX_HOLD_CANDLES = 24
SLIPPAGE = 0.0005
MAX_WORKERS = 4


@dataclass(frozen=True)
class SignalParams:
    """Parameters for signal evaluation."""

    rsi_oversold: int = 30
    rsi_overbought: int = 70
    adx_threshold: int = 20
    pattern_confirm_buy_rsi: int = 45
    pattern_confirm_sell_rsi: int = 55
    sentiment_buy_threshold: float = 0.3
    sentiment_sell_threshold: float = -0.3


@dataclass(frozen=True)
class SignalResult:
    """Result of signal evaluation for a single candle."""

    action: Literal["BUY", "SELL", "HOLD"]
    rsi: float
    adx: float
    pattern_bias: int  # 0=neutral, 1=bullish, 2=bearish
    sentiment: float


DEFAULT_PARAMS = SignalParams()


def evaluate_signals(
    rsi: NDArray[np.float64],
    adx: NDArray[np.float64],
    pattern_bias: NDArray[np.int8],
    sentiment: NDArray[np.float64] | None = None,
    params: SignalParams = DEFAULT_PARAMS,
) -> NDArray[np.int8]:
    """Evaluate signals for each candle (vectorized).

    Returns:
        int8 array: 0=HOLD, 1=BUY, 2=SELL
    """
    n = len(rsi)
    actions = np.zeros(n, dtype=np.int8)

    if sentiment is None:
        sentiment = np.zeros(n, dtype=np.float64)

    # Neutral sentiment (original logic)
    neutral_mask = (sentiment <= params.sentiment_buy_threshold) & (
        sentiment >= params.sentiment_sell_threshold
    )

    # ADX trend filter
    adx_ok = adx >= params.adx_threshold

    # Pattern-confirmed BUY
    buy_mask = neutral_mask & adx_ok & (pattern_bias == 1) & (rsi < params.pattern_confirm_buy_rsi)

    # Pattern-confirmed SELL
    sell_mask = (
        neutral_mask & adx_ok & (pattern_bias == 2) & (rsi > params.pattern_confirm_sell_rsi)
    )

    # Sentiment-driven BUY (sentiment > 0.3)
    sentiment_buy_mask = (
        (sentiment > params.sentiment_buy_threshold) & adx_ok & (rsi < params.rsi_oversold)
    )

    # Sentiment-driven SELL (sentiment < -0.3)
    sentiment_sell_mask = (
        (sentiment < params.sentiment_sell_threshold) & adx_ok & (rsi > params.rsi_overbought)
    )

    actions[buy_mask | sentiment_buy_mask] = 1  # BUY
    actions[sell_mask | sentiment_sell_mask] = 2  # SELL

    # NaN handling
    actions[np.isnan(rsi) | np.isnan(adx)] = 0

    return actions


def evaluate_signals_loop(
    prepared: dict,
    params: SignalParams = DEFAULT_PARAMS,
    sentiment: float = 0.0,
    index_range: tuple[int, int] | None = None,
) -> list[dict]:
    """Original per-candle loop evaluation (for verification)."""
    n = prepared["length"]
    start = max(30, index_range[0] if index_range else 0)
    end = min(n - 1, index_range[1] if index_range else n - 1)

    trades = []
    for i in range(start, end):
        rsi_val = prepared["rsis"][i]
        adx_val = prepared["adxs"][i]
        pattern = prepared["pattern_bias"][i] if "pattern_bias" in prepared else 0

        if np.isnan(rsi_val) or np.isnan(adx_val):
            continue

        action = 0  # HOLD

        if sentiment == 0.0:
            # Neutral sentiment - pattern confirmed
            if adx_val >= params.adx_threshold:
                if pattern == 1 and rsi_val < params.pattern_confirm_buy_rsi:
                    action = 1  # BUY
                elif pattern == 2 and rsi_val > params.pattern_confirm_sell_rsi:
                    action = 2  # SELL
        elif sentiment > params.sentiment_buy_threshold:
            # Bullish sentiment
            if adx_val >= params.adx_threshold and rsi_val < params.rsi_oversold:
                action = 1  # BUY
        elif sentiment < params.sentiment_sell_threshold:
            # Bearish sentiment
            if adx_val >= params.adx_threshold and rsi_val > params.rsi_overbought:
                action = 2  # SELL

        if action != 0:
            trades.append(
                {
                    "index": i,
                    "action": "BUY" if action == 1 else "SELL",
                    "rsi": float(rsi_val),
                    "adx": float(adx_val),
                    "pattern": int(pattern),
                }
            )

    return trades


def prepare_symbol_data(
    df: pd.DataFrame,
    rsi_period: int = 14,
    adx_period: int = 14,
    atr_period: int = 14,
    swing_order: int = 5,
) -> dict:
    """Prepare all data needed for signal evaluation."""
    indicators = prepare_indicators(df, rsi_period, adx_period, atr_period, swing_order)

    pattern_bias = compute_pattern_bias(
        indicators["opens"],
        indicators["highs"],
        indicators["lows"],
        indicators["closes"],
        swing_order,
    )

    return {
        **indicators,
        "pattern_bias": pattern_bias,
        "symbol": df.attrs.get("symbol", "UNKNOWN"),
        "timestamps": df["timestamp"].to_numpy()
        if "timestamp" in df.columns
        else np.arange(len(df)),
    }


# Import pandas for type hint
import pandas as pd


def prepare_symbol_series(symbol: str, candle_limit: int = 20000) -> dict | None:
    """Prepare symbol data for backtesting (matches original backtest.py interface)."""
    from trading_agent.storage.parquet_store import get_parquet_store

    parquet = get_parquet_store()
    df = parquet.read_latest(symbol, "5minute", n=candle_limit)

    if df is None or df.empty or len(df) < LOOKBACK_MIN:
        return None

    return prepare_symbol_data(df)


def simulate_trades(prepared: dict, params: dict | None = None) -> list:
    """Simulate trades using vectorized evaluation (matches original backtest.py interface)."""
    # DEFAULT_PARAMS is a SignalParams dataclass; callers may pass overrides as a dict.
    params = {**asdict(DEFAULT_PARAMS), **(params or {})}

    n = prepared["length"]
    rsi = prepared["rsis"]
    adx = prepared["adxs"]
    pattern_bias = prepared["pattern_bias"]
    timestamps = prepared["timestamps"]
    closes = prepared["closes"]
    opens = prepared["opens"]

    # Evaluate signals
    actions = np.zeros(n, dtype=np.int8)

    with np.errstate(invalid="ignore"):
        adx_ok = adx >= params["adx_threshold"]
        buy = adx_ok & (pattern_bias == 1) & (rsi < params["pattern_confirm_buy_rsi"])
        sell = adx_ok & (pattern_bias == 2) & (rsi > params["pattern_confirm_sell_rsi"])

    actions[sell] = 2
    actions[buy] = 1
    actions[np.isnan(rsi) | np.isnan(adx)] = 0

    trade_idx = np.nonzero(actions != 0)[0]
    if len(trade_idx) == 0:
        return []

    trades = []
    for i in trade_idx:
        if i + FORWARD_WINDOW >= n:
            continue

        entry_price = opens[i + 1] if i + 1 < n else closes[i]
        exit_price = closes[i + FORWARD_WINDOW]

        pct_return = (exit_price - entry_price) / entry_price
        if actions[i] == 2:
            pct_return = -pct_return

        trades.append({
            "symbol": prepared.get("symbol", "UNKNOWN"),
            "timestamp": timestamps[i],
            "action": "BUY" if actions[i] == 1 else "SELL",
            "entry": entry_price,
            "exit": exit_price,
            "return_pct": pct_return,
        })

    return trades


def summarize(label: str, trades: list) -> dict:
    """Summarize trades (matches original backtest.py interface)."""
    if not trades:
        print(f"{label}: no signals fired - thresholds may be too strict, or too little history.")
        return {"label": label, "trades": 0}

    returns = pd.Series([t["return_pct"] for t in trades])
    wins = returns[returns > 0]
    losses = returns[returns <= 0]
    win_rate = len(wins) / len(returns)
    avg_win = wins.mean() if len(wins) else 0.0
    avg_loss = losses.mean() if len(losses) else 0.0
    risk_reward = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")
    sharpe = returns.mean() / returns.std() if returns.std() > 0 else 0.0

    # Additive cumulative curve for drawdown
    cumulative_additive = returns.cumsum()
    running_max = cumulative_additive.cummax()
    drawdown = cumulative_additive - running_max
    max_drawdown = drawdown.min()

    # Exit reason breakdown
    exit_reasons = {}
    for t in trades:
        reason = t.get("exit_reason", "UNKNOWN")
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1

    result = {
        "label": label,
        "trades": len(trades),
        "win_rate": round(win_rate * 100, 1),
        "avg_win_pct": round(avg_win * 100, 3),
        "avg_loss_pct": round(avg_loss * 100, 3),
        "risk_reward_ratio": round(risk_reward, 2),
        "sharpe_like_ratio": round(sharpe, 3),
        "max_drawdown_pct": round(max_drawdown * 100, 2),
        "total_return_pct": round(returns.sum() * 100, 2),
        "exit_reasons": exit_reasons,
    }

    print(f"\n--- Backtest results: {label} ---")
    for k, v in result.items():
        print(f"  {k}: {v}")
    return result


def verify_fast_path_matches_reference(symbol: str = "RELIANCE", candle_limit: int = 2000) -> bool:
    """Check the vectorized walk-forward path against the slow loop for ``symbol``.

    Delegates to the walk-forward selftest rather than comparing the fast path
    with itself. False when ``symbol`` has no stored history (nothing verified).
    """
    from trading_agent.research.walk_forward_optimizer import run_selftest

    return run_selftest([symbol], synthetic_fallback=False)
