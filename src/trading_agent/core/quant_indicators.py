"""Quantitative technical indicators - Vectorized implementations using NumPy/Pandas."""

from __future__ import annotations

import numpy as np
import pandas as pd
from numpy.typing import NDArray


def sma(values: NDArray[np.float64], period: int) -> NDArray[np.float64]:
    """Simple Moving Average."""
    if period <= 0:
        raise ValueError("Period must be positive")
    return pd.Series(values).rolling(period, min_periods=period).mean().to_numpy()


def ema(values: NDArray[np.float64], period: int) -> NDArray[np.float64]:
    """Exponential Moving Average."""
    if period <= 0:
        raise ValueError("Period must be positive")
    alpha = 2.0 / (period + 1)
    return pd.Series(values).ewm(alpha=alpha, adjust=False, min_periods=1).mean().to_numpy()


def rsi(values: NDArray[np.float64], period: int = 14) -> NDArray[np.float64]:
    """Relative Strength Index (Wilder's smoothing)."""
    if period <= 0:
        raise ValueError("Period must be positive")
    if len(values) < period + 1:
        return np.full(len(values), np.nan)

    deltas = np.diff(values)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    # Wilder's smoothing (EMA with alpha=1/period)
    avg_gain = pd.Series(gains).ewm(alpha=1 / period, adjust=False, min_periods=1).mean()
    avg_loss = pd.Series(losses).ewm(alpha=1 / period, adjust=False, min_periods=1).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi_vals = 100 - (100 / (1 + rs))

    # Prepend NaN for first value (no delta)
    return np.concatenate([[np.nan], rsi_vals.to_numpy()])


def atr(
    high: NDArray[np.float64],
    low: NDArray[np.float64],
    close: NDArray[np.float64],
    period: int = 14,
) -> NDArray[np.float64]:
    """Average True Range (Wilder's smoothing)."""
    if period <= 0:
        raise ValueError("Period must be positive")

    prev_close = np.roll(close, 1)
    prev_close[0] = np.nan

    tr1 = high - low
    tr2 = np.abs(high - prev_close)
    tr3 = np.abs(low - prev_close)

    true_range = np.maximum(np.maximum(tr1, tr2), tr3)
    true_range[0] = np.nan

    return (
        pd.Series(true_range).ewm(alpha=1 / period, adjust=False, min_periods=1).mean().to_numpy()
    )


def adx(
    high: NDArray[np.float64],
    low: NDArray[np.float64],
    close: NDArray[np.float64],
    period: int = 14,
) -> NDArray[np.float64]:
    """Average Directional Index."""
    if period <= 0:
        raise ValueError("Period must be positive")

    n = len(high)
    if n < period + 1:
        return np.full(n, np.nan)

    # Directional Movement
    up_move = np.diff(high, prepend=high[0])
    down_move = -np.diff(low, prepend=low[0])

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    # True Range
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr1 = high - low
    tr2 = np.abs(high - prev_close)
    tr3 = np.abs(low - prev_close)
    true_range = np.maximum(np.maximum(tr1, tr2), tr3)

    # Wilder's smoothing - use min_periods=1 to avoid NaN prefix
    atr_series = pd.Series(true_range).ewm(alpha=1 / period, adjust=False, min_periods=1).mean()
    plus_di = (
        100
        * pd.Series(plus_dm).ewm(alpha=1 / period, adjust=False, min_periods=1).mean()
        / atr_series
    )
    minus_di = (
        100
        * pd.Series(minus_dm).ewm(alpha=1 / period, adjust=False, min_periods=1).mean()
        / atr_series
    )

    dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)
    adx_vals = pd.Series(dx).ewm(alpha=1 / period, adjust=False, min_periods=1).mean()

    # Fill NaN at the start with 0 for ADX (no trend at start)
    result = adx_vals.to_numpy()
    result = np.nan_to_num(result, nan=0.0)
    return result


def bollinger_bands(
    values: NDArray[np.float64],
    period: int = 20,
    num_std: float = 2.0,
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
    """Bollinger Bands - returns (upper, middle, lower)."""
    if period <= 0:
        raise ValueError("Period must be positive")

    middle = sma(values, period)
    std = pd.Series(values).rolling(period, min_periods=period).std().to_numpy()
    upper = middle + num_std * std
    lower = middle - num_std * std

    return upper, middle, lower


def macd(
    values: NDArray[np.float64],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
    """MACD - returns (macd_line, signal_line, histogram)."""
    if fast_period >= slow_period:
        raise ValueError("Fast period must be less than slow period")

    fast_ema = ema(values, fast_period)
    slow_ema = ema(values, slow_period)
    macd_line = fast_ema - slow_ema
    signal_line = ema(macd_line, signal_period)
    histogram = macd_line - signal_line

    return macd_line, signal_line, histogram


def stochastic(
    high: NDArray[np.float64],
    low: NDArray[np.float64],
    close: NDArray[np.float64],
    k_period: int = 14,
    d_period: int = 3,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Stochastic Oscillator - returns (%K, %D)."""
    if k_period <= 0 or d_period <= 0:
        raise ValueError("Periods must be positive")

    lowest_low = pd.Series(low).rolling(k_period, min_periods=k_period).min()
    highest_high = pd.Series(high).rolling(k_period, min_periods=k_period).max()

    k = 100 * (close - lowest_low) / (highest_high - lowest_low).replace(0, np.nan)
    d = pd.Series(k).rolling(d_period, min_periods=d_period).mean()

    return k.to_numpy(), d.to_numpy()


def williams_r(
    high: NDArray[np.float64],
    low: NDArray[np.float64],
    close: NDArray[np.float64],
    period: int = 14,
) -> NDArray[np.float64]:
    """Williams %R in [-100, 0]; below -80 oversold, above -20 overbought."""
    if period <= 0:
        raise ValueError("Period must be positive")
    highest = pd.Series(high).rolling(period, min_periods=period).max()
    lowest = pd.Series(low).rolling(period, min_periods=period).min()
    return (-100 * (highest - close) / (highest - lowest).replace(0, np.nan)).to_numpy()


def cci(
    high: NDArray[np.float64],
    low: NDArray[np.float64],
    close: NDArray[np.float64],
    period: int = 20,
) -> NDArray[np.float64]:
    """Commodity Channel Index (Lambert, 0.015 constant)."""
    if period <= 0:
        raise ValueError("Period must be positive")
    typical = pd.Series((high + low + close) / 3)
    mean = typical.rolling(period, min_periods=period).mean()
    mean_dev = typical.rolling(period, min_periods=period).apply(
        lambda w: np.mean(np.abs(w - w.mean())), raw=True
    )
    return ((typical - mean) / (0.015 * mean_dev.replace(0, np.nan))).to_numpy()


def directional_indicators(
    high: NDArray[np.float64],
    low: NDArray[np.float64],
    close: NDArray[np.float64],
    period: int = 14,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """+DI and -DI (Wilder), the same smoothing adx() uses internally."""
    if period <= 0:
        raise ValueError("Period must be positive")
    up_move = np.diff(high, prepend=high[0])
    down_move = -np.diff(low, prepend=low[0])
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    atr_series = pd.Series(true_range(high, low, close)).ewm(
        alpha=1 / period, adjust=False, min_periods=1
    ).mean().replace(0, np.nan)
    smooth = lambda dm: pd.Series(dm).ewm(alpha=1 / period, adjust=False, min_periods=1).mean()  # noqa: E731
    return (100 * smooth(plus_dm) / atr_series).to_numpy(), (100 * smooth(minus_dm) / atr_series).to_numpy()


def donchian_channels(
    high: NDArray[np.float64],
    low: NDArray[np.float64],
    period: int = 20,
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
    """Donchian Channels - returns (upper, middle, lower)."""
    if period <= 0:
        raise ValueError("Period must be positive")

    upper = pd.Series(high).rolling(period, min_periods=period).max().to_numpy()
    lower = pd.Series(low).rolling(period, min_periods=period).min().to_numpy()
    middle = (upper + lower) / 2

    return upper, middle, lower


def vwap(
    high: NDArray[np.float64],
    low: NDArray[np.float64],
    close: NDArray[np.float64],
    volume: NDArray[np.float64],
) -> NDArray[np.float64]:
    """Volume Weighted Average Price (session VWAP)."""
    typical_price = (high + low + close) / 3
    cum_vol = np.cumsum(volume)
    cum_pv = np.cumsum(typical_price * volume)
    return np.where(cum_vol > 0, cum_pv / cum_vol, np.nan)


def true_range(
    high: NDArray[np.float64],
    low: NDArray[np.float64],
    close: NDArray[np.float64],
) -> NDArray[np.float64]:
    """True Range for a single period."""
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr1 = high - low
    tr2 = np.abs(high - prev_close)
    tr3 = np.abs(low - prev_close)
    return np.maximum(np.maximum(tr1, tr2), tr3)


def swings(
    high: NDArray[np.float64],
    low: NDArray[np.float64],
    order: int = 5,
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Find swing highs and lows using local extrema.

    Returns:
        Tuple of (swing_highs, swing_lows) where each is a list of (index, price).
    """
    from scipy.signal import argrelextrema

    high_indices = argrelextrema(high, np.greater_equal, order=order)[0]
    low_indices = argrelextrema(low, np.less_equal, order=order)[0]

    swing_highs = [(int(i), float(high[i])) for i in high_indices]
    swing_lows = [(int(i), float(low[i])) for i in low_indices]

    return swing_highs, swing_lows


def pattern_detection(
    open_: NDArray[np.float64],
    high: NDArray[np.float64],
    low: NDArray[np.float64],
    close: NDArray[np.float64],
    order: int = 5,
    tolerance: float = 0.015,
) -> tuple[NDArray[np.bool_], NDArray[np.bool_], NDArray[np.bool_]]:
    """Detect candlestick patterns.

    Returns:
        Tuple of (hammers, bullish_engulfing, bearish_engulfing) as boolean arrays.
    """
    n = len(close)
    hammers = np.zeros(n, dtype=bool)
    bull_engulf = np.zeros(n, dtype=bool)
    bear_engulf = np.zeros(n, dtype=bool)

    for i in range(1, n):
        # Hammer: small body, long lower shadow, little/no upper shadow
        body = abs(close[i] - open_[i])
        lower_shadow = min(open_[i], close[i]) - low[i]
        upper_shadow = high[i] - max(open_[i], close[i])
        if body > 0 and lower_shadow >= 2 * body and upper_shadow <= 0.1 * body:
            hammers[i] = True

        # Bullish Engulfing: prior bearish, current bullish engulfs prior
        if close[i - 1] < open_[i - 1] and close[i] > open_[i]:
            if open_[i] <= close[i - 1] and close[i] >= open_[i - 1]:
                bull_engulf[i] = True

        # Bearish Engulfing: prior bullish, current bearish engulfs prior
        if close[i - 1] > open_[i - 1] and close[i] < open_[i]:
            if open_[i] >= close[i - 1] and close[i] <= open_[i - 1]:
                bear_engulf[i] = True

    # Swing patterns (double top/bottom)
    swing_highs, swing_lows = swings(high, low, order)

    return hammers, bull_engulf, bear_engulf


def prepare_indicators(
    df: pd.DataFrame,
    rsi_period: int = 14,
    adx_period: int = 14,
    atr_period: int = 14,
    swing_order: int = 5,
) -> dict[str, NDArray[np.float64] | NDArray[np.bool_]]:
    """Prepare all indicators for a symbol DataFrame.

    Args:
        df: DataFrame with columns [timestamp, open, high, low, close, volume]
        rsi_period: RSI period
        adx_period: ADX period
        atr_period: ATR period
        swing_order: Swing detection order

    Returns:
        Dictionary with all indicator arrays.
    """
    required = ["open", "high", "low", "close", "volume"]
    for col in required:
        if col not in df.columns:
            raise ValueError(f"Missing required column: {col}")

    o = df["open"].to_numpy(dtype=np.float64)
    h = df["high"].to_numpy(dtype=np.float64)
    l = df["low"].to_numpy(dtype=np.float64)
    c = df["close"].to_numpy(dtype=np.float64)
    v = df["volume"].to_numpy(dtype=np.float64)

    hammers, bull_engulf, bear_engulf = pattern_detection(o, h, l, c, swing_order)
    swing_highs, swing_lows = swings(h, l, swing_order)

    return {
        "opens": o,
        "highs": h,
        "lows": l,
        "closes": c,
        "volumes": v,
        "rsis": rsi(c, rsi_period),
        "adxs": adx(h, l, c, adx_period),
        "atrs": atr(h, l, c, atr_period),
        "hammers": hammers,
        "bull_engulf": bull_engulf,
        "bear_engulf": bear_engulf,
        "swing_highs": swing_highs,
        "swing_lows": swing_lows,
        "length": len(c),
    }
