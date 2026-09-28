"""
Deterministic technical indicators, computed straight from the candles
table. No LLM involved here on purpose - this is fast, free, and exact.
"""
import pandas as pd
from storage.db import get_connection


def load_candles(symbol: str, limit: int = 200) -> pd.DataFrame:
    conn = get_connection()
    df = pd.read_sql_query(
        "SELECT * FROM candles WHERE symbol = ? ORDER BY timestamp DESC LIMIT ?",
        conn, params=(symbol, limit),
    )
    conn.close()
    return df.iloc[::-1].reset_index(drop=True)  # chronological order


def compute_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    delta = df["close"].diff()
    # Replace simple rolling mean with Exponential Weighted Math (alpha = 1/period)
    gain = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    loss = -delta.clip(upper=0).ewm(alpha=1/period, adjust=False).mean()
    rs = gain / loss.replace(0, 1e-9)
    return 100 - (100 / (1 + rs))


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return true_range.rolling(period).mean()


def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    up_move = df["high"].diff()
    down_move = -df["low"].diff()
    plus_dm = ((up_move > down_move) & (up_move > 0)) * up_move
    minus_dm = ((down_move > up_move) & (down_move > 0)) * down_move
    atr = compute_atr(df, period).replace(0, 1e-9)
    
    # Replace simple rolling mean with Exponential Weighted Math
    plus_di = 100 * plus_dm.ewm(alpha=1/period, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1/period, adjust=False).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-9)
    return dx.ewm(alpha=1/period, adjust=False).mean()


def compute_di(df: pd.DataFrame, period: int = 14) -> tuple:
    """+DI/-DI (directional indicators) - ADX measures trend STRENGTH
    only, not direction; +DI vs -DI is what gives the direction. Kept
    separate from compute_adx() rather than changing its return type,
    since existing callers (backtest.py, decision_agent.py) depend on
    compute_adx() returning a single Series."""
    up_move = df["high"].diff()
    down_move = -df["low"].diff()
    plus_dm = ((up_move > down_move) & (up_move > 0)) * up_move
    minus_dm = ((down_move > up_move) & (down_move > 0)) * down_move
    atr = compute_atr(df, period).replace(0, 1e-9)
    plus_di = 100 * plus_dm.rolling(period).mean() / atr
    minus_di = 100 * minus_dm.rolling(period).mean() / atr
    return plus_di, minus_di


def compute_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple:
    """Returns (macd_line, signal_line, histogram). Standard EMA-based
    MACD - histogram = macd_line - signal_line; sign of the histogram
    is what most platforms (including Groww's Technicals tab) use for
    the bullish/bearish verdict."""
    ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def compute_stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3) -> tuple:
    """Returns (%K, %D). Same reversal framing as RSI: <20 = oversold
    (bullish reversal read), >80 = overbought (bearish reversal read)."""
    low_min = df["low"].rolling(k_period).min()
    high_max = df["high"].rolling(k_period).max()
    denom = (high_max - low_min).replace(0, 1e-9)
    percent_k = 100 * (df["close"] - low_min) / denom
    percent_d = percent_k.rolling(d_period).mean()
    return percent_k, percent_d


def compute_williams_r(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Same reversal framing as RSI/Stochastic, just on a -100..0 scale:
    below -80 = oversold (bullish read), above -20 = overbought (bearish read)."""
    high_max = df["high"].rolling(period).max()
    low_min = df["low"].rolling(period).min()
    denom = (high_max - low_min).replace(0, 1e-9)
    return -100 * (high_max - df["close"]) / denom


def compute_cci(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """CCI uses the OPPOSITE convention from RSI/Stochastic/Williams %R -
    it's read as MOMENTUM CONTINUATION, not reversal: above +100 =
    bullish (strong upward momentum), below -100 = bearish. This is a
    genuinely different, commonly-used convention - not a mistake to
    reconcile with the others."""
    import numpy as np
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    sma = typical_price.rolling(period).mean()
    mean_deviation = typical_price.rolling(period).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    return (typical_price - sma) / (0.015 * mean_deviation.replace(0, 1e-9))


def indicators_from_df(df: pd.DataFrame) -> dict:
    """Same as latest_indicators() but operates on an already-loaded
    DataFrame slice, so backtesting can call this on historical
    windows without hitting the DB each time."""
    if len(df) < 20:
        return {"error": "not enough candles yet"}

    df = df.copy()
    df["rsi"] = compute_rsi(df)
    df["atr"] = compute_atr(df)
    df["adx"] = compute_adx(df)
    last = df.iloc[-1]

    return {
        "close": last["close"],
        "rsi": round(last["rsi"], 2) if pd.notna(last["rsi"]) else None,
        "atr": round(last["atr"], 2) if pd.notna(last["atr"]) else None,
        "adx": round(last["adx"], 2) if pd.notna(last["adx"]) else None,
        # The candle these indicators were computed from. Callers log
        # signals against THIS rather than wall-clock now(), so a signal
        # row always lines up with a candle instead of landing on an
        # arbitrary second between bars.
        "timestamp": last["timestamp"],
    }


def latest_indicators(symbol: str) -> dict:
    df = load_candles(symbol)
    if len(df) < 20:
        return {"symbol": symbol, "error": "not enough candles yet"}
    result = indicators_from_df(df)
    result["symbol"] = symbol
    return result


if __name__ == "__main__":
    print(latest_indicators("RELIANCE"))
