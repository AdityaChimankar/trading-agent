"""
Deterministic chart pattern detection - candlestick patterns (single/
multi-candle) plus swing-based patterns (double top/bottom). Pure
pandas/scipy, no TA-Lib C dependency needed, so it stays easy to
install and runs fast enough for a 5-min decision cycle.
"""
import pandas as pd
from scipy.signal import argrelextrema
from quant_indicators import load_candles


def _body(df):
    return (df["close"] - df["open"]).abs()


def _range(df):
    return df["high"] - df["low"]


def detect_doji(df: pd.DataFrame, threshold: float = 0.1) -> bool:
    """Very small body relative to range = indecision."""
    last = df.iloc[-1]
    body = abs(last["close"] - last["open"])
    rng = last["high"] - last["low"]
    return rng > 0 and (body / rng) < threshold


def detect_hammer(df: pd.DataFrame) -> bool:
    """Small body near the top, long lower wick = potential bullish reversal."""
    last = df.iloc[-1]
    body = abs(last["close"] - last["open"])
    lower_wick = min(last["open"], last["close"]) - last["low"]
    upper_wick = last["high"] - max(last["open"], last["close"])
    return body > 0 and lower_wick > 2 * body and upper_wick < body


def detect_bullish_engulfing(df: pd.DataFrame) -> bool:
    if len(df) < 2:
        return False
    prev, last = df.iloc[-2], df.iloc[-1]
    prev_bearish = prev["close"] < prev["open"]
    last_bullish = last["close"] > last["open"]
    engulfs = last["open"] <= prev["close"] and last["close"] >= prev["open"]
    return prev_bearish and last_bullish and engulfs


def detect_bearish_engulfing(df: pd.DataFrame) -> bool:
    if len(df) < 2:
        return False
    prev, last = df.iloc[-2], df.iloc[-1]
    prev_bullish = prev["close"] > prev["open"]
    last_bearish = last["close"] < last["open"]
    engulfs = last["open"] >= prev["close"] and last["close"] <= prev["open"]
    return prev_bullish and last_bearish and engulfs


def detect_double_top_bottom(df: pd.DataFrame, order: int = 5, tolerance: float = 0.015) -> str | None:
    """
    Finds recent swing highs/lows and checks if the last two are near-equal
    (within tolerance %) - the classic double top/bottom signature.
    """
    if len(df) < order * 4:
        return None

    highs_idx = argrelextrema(df["high"].values, lambda a, b: a >= b, order=order)[0]
    lows_idx = argrelextrema(df["low"].values, lambda a, b: a <= b, order=order)[0]

    if len(highs_idx) >= 2:
        h1, h2 = df["high"].iloc[highs_idx[-2]], df["high"].iloc[highs_idx[-1]]
        if abs(h1 - h2) / h1 < tolerance:
            return "double_top"  # bearish signal

    if len(lows_idx) >= 2:
        l1, l2 = df["low"].iloc[lows_idx[-2]], df["low"].iloc[lows_idx[-1]]
        if abs(l1 - l2) / l1 < tolerance:
            return "double_bottom"  # bullish signal

    return None


def patterns_from_df(df: pd.DataFrame) -> dict:
    """Same as detect_patterns() but on an already-loaded DataFrame
    slice, for backtesting on historical windows without DB hits."""
    if len(df) < 20:
        return {"patterns": [], "bias": "neutral"}

    found = []
    if detect_doji(df):
        found.append("doji")
    if detect_hammer(df):
        found.append("hammer")
    if detect_bullish_engulfing(df):
        found.append("bullish_engulfing")
    if detect_bearish_engulfing(df):
        found.append("bearish_engulfing")

    swing = detect_double_top_bottom(df)
    if swing:
        found.append(swing)

    bullish = {"hammer", "bullish_engulfing", "double_bottom"}
    bearish = {"bearish_engulfing", "double_top"}
    bias = "neutral"
    if any(p in bullish for p in found):
        bias = "bullish"
    elif any(p in bearish for p in found):
        bias = "bearish"

    return {"patterns": found, "bias": bias}


def compute_pattern_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Vectorized version of the single-candle pattern checks, computed
    once over the whole series instead of per-step. Each row only looks
    at itself and the previous row (shift(1)), so this is fully causal -
    identical results to calling the per-step functions, just O(n) total
    instead of O(n) work repeated at every one of n steps."""
    df = df.copy()
    body = (df["close"] - df["open"]).abs()
    rng = df["high"] - df["low"]
    lower_wick = df[["open", "close"]].min(axis=1) - df["low"]
    upper_wick = df["high"] - df[["open", "close"]].max(axis=1)

    df["doji"] = (rng > 0) & ((body / rng) < 0.1)
    df["hammer"] = (body > 0) & (lower_wick > 2 * body) & (upper_wick < body)

    prev_open, prev_close = df["open"].shift(1), df["close"].shift(1)
    prev_bearish = prev_close < prev_open
    prev_bullish = prev_close > prev_open
    last_bullish = df["close"] > df["open"]
    last_bearish = df["close"] < df["open"]

    df["bullish_engulfing"] = prev_bearish & last_bullish & (df["open"] <= prev_close) & (df["close"] >= prev_open)
    df["bearish_engulfing"] = prev_bullish & last_bearish & (df["open"] >= prev_close) & (df["close"] <= prev_open)
    return df


def compute_swing_extrema(df: pd.DataFrame, order: int = 5) -> dict:
    """
    Precomputes swing highs/lows ONCE for the whole series using
    argrelextrema. A swing point at index j only becomes usable once
    `order` bars have passed after j (that's what confirms it as a
    local extreme) - so at backtest step i, only swings with
    j + order <= i are causally valid to use. This precomputation
    itself is O(n) total, done once instead of recomputed per-step.
    """
    if len(df) < order * 2 + 1:
        return {"highs": [], "lows": []}
    highs_idx = argrelextrema(df["high"].values, lambda a, b: a >= b, order=order)[0]
    lows_idx = argrelextrema(df["low"].values, lambda a, b: a <= b, order=order)[0]
    return {
        "highs": [(int(j), df["high"].iloc[j]) for j in highs_idx],
        "lows": [(int(j), df["low"].iloc[j]) for j in lows_idx],
    }


def swing_bias_at(swings: dict, i: int, order: int, tolerance: float = 0.015) -> str | None:
    """Causal lookup: which double top/bottom (if any) is confirmed as of step i."""
    valid_highs = [(j, p) for j, p in swings["highs"] if j + order <= i]
    valid_lows = [(j, p) for j, p in swings["lows"] if j + order <= i]

    if len(valid_highs) >= 2:
        (_, h1), (_, h2) = valid_highs[-2], valid_highs[-1]
        if abs(h1 - h2) / h1 < tolerance:
            return "double_top"
    if len(valid_lows) >= 2:
        (_, l1), (_, l2) = valid_lows[-2], valid_lows[-1]
        if abs(l1 - l2) / l1 < tolerance:
            return "double_bottom"
    return None


def detect_patterns(symbol: str) -> dict:
    """Run all pattern detectors on the latest candles for a symbol."""
    df = load_candles(symbol, limit=100)
    result = patterns_from_df(df)
    result["symbol"] = symbol
    return result


if __name__ == "__main__":
    print(detect_patterns("RELIANCE"))
