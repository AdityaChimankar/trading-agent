"""Pattern detection utilities for candlestick and chart patterns."""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True)
class PatternBias:
    """Pattern bias for a single candle."""

    candle_index: int
    bias: Literal["bullish", "bearish", "neutral"]
    patterns: list[str]


def detect_single_candle_patterns(
    open_: NDArray[np.float64],
    high: NDArray[np.float64],
    low: NDArray[np.float64],
    close: NDArray[np.float64],
) -> tuple[NDArray[np.bool_], NDArray[np.bool_], NDArray[np.bool_]]:
    """Detect single-candle patterns.

    Returns:
        Tuple of (hammers, bullish_engulfing, bearish_engulfing).
    """
    n = len(close)
    hammers = np.zeros(n, dtype=bool)
    bull_engulf = np.zeros(n, dtype=bool)
    bear_engulf = np.zeros(n, dtype=bool)

    for i in range(1, n):
        body = abs(close[i] - open_[i])
        lower_shadow = min(open_[i], close[i]) - low[i]
        upper_shadow = high[i] - max(open_[i], close[i])

        # Hammer: small body, long lower shadow, little/no upper shadow
        if body > 0 and lower_shadow >= 2 * body and upper_shadow <= 0.1 * body:
            hammers[i] = True

        # Bullish Engulfing
        if close[i - 1] < open_[i - 1] and close[i] > open_[i]:
            if open_[i] <= close[i - 1] and close[i] >= open_[i - 1]:
                bull_engulf[i] = True

        # Bearish Engulfing
        if close[i - 1] > open_[i - 1] and close[i] < open_[i]:
            if open_[i] >= close[i - 1] and close[i] <= open_[i - 1]:
                bear_engulf[i] = True

    return hammers, bull_engulf, bear_engulf


def detect_doji(
    open_: NDArray[np.float64],
    high: NDArray[np.float64],
    low: NDArray[np.float64],
    close: NDArray[np.float64],
    body_ratio: float = 0.1,
) -> NDArray[np.bool_]:
    """Doji: body no more than ``body_ratio`` of the candle's range (indecision)."""
    rng = high - low
    body = np.abs(close - open_)
    return (rng > 0) & (body <= body_ratio * rng)


def find_swings(
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


def detect_swing_patterns(
    high: NDArray[np.float64],
    low: NDArray[np.float64],
    order: int = 5,
    tolerance: float = 0.015,
) -> tuple[NDArray[np.object_], NDArray[np.object_]]:
    """Detect double top and double bottom patterns at each candle.

    Returns:
        Tuple of (swing_bias_at_high, swing_bias_at_low) where each element
        is either None, 'double_top', or 'double_bottom'.
    """
    n = len(high)
    swing_highs, swing_lows = find_swings(high, low, order)

    high_idxs = [j for j, _ in swing_highs]
    high_prices = [p for _, p in swing_highs]
    low_idxs = [j for j, _ in swing_lows]
    low_prices = [p for _, p in swing_lows]

    swing_at_high = np.empty(n, dtype=object)
    swing_at_low = np.empty(n, dtype=object)
    swing_at_high[:] = None
    swing_at_low[:] = None

    for i in range(n):
        cutoff = i - order

        # Check double top
        h_count = bisect.bisect_right(high_idxs, cutoff)
        if h_count >= 2:
            h1, h2 = high_prices[h_count - 2], high_prices[h_count - 1]
            if abs(h1 - h2) / h1 < tolerance:
                swing_at_high[i] = "double_top"

        # Check double bottom
        l_count = bisect.bisect_right(low_idxs, cutoff)
        if l_count >= 2:
            l1, l2 = low_prices[l_count - 2], low_prices[l_count - 1]
            if abs(l1 - l2) / l1 < tolerance:
                swing_at_low[i] = "double_bottom"

    return swing_at_high, swing_at_low


def compute_pattern_bias(
    open_: NDArray[np.float64],
    high: NDArray[np.float64],
    low: NDArray[np.float64],
    close: NDArray[np.float64],
    order: int = 5,
    tolerance: float = 0.015,
) -> NDArray[np.int8]:
    """Compute overall pattern bias for each candle.

    Returns:
        int8 array: 0=neutral, 1=bullish, 2=bearish

    Precedence (matching original logic):
    1. Single-candle: bearish_engulfing set first, then hammer/bullish_engulfing overwrite
    2. Swing: double_top checked BEFORE double_bottom with early return
    3. Overall: bullish wins if ANY bullish signal present
    """
    n = len(close)
    hammers, bull_engulf, bear_engulf = detect_single_candle_patterns(open_, high, low, close)
    swing_at_high, swing_at_low = detect_swing_patterns(high, low, order, tolerance)

    codes = np.zeros(n, dtype=np.int8)

    for i in range(n):
        # Single-candle patterns (bearish set first, then bullish overwrites)
        bullish = bool(hammers[i]) or bool(bull_engulf[i])
        bearish = bool(bear_engulf[i])

        # Swing patterns (double_top checked first, early return if found)
        swing = None
        if swing_at_high[i] == "double_top":
            swing = "double_top"
        elif swing_at_low[i] == "double_bottom":
            swing = "double_bottom"

        # Combine: swing result only contributes alongside single-candle
        if swing == "double_bottom":
            bullish = True
        elif swing == "double_top":
            bearish = True

        # Overall: bullish wins if ANY bullish signal present
        if bullish:
            codes[i] = 1
        elif bearish:
            codes[i] = 2
        # else neutral (0)

    return codes
