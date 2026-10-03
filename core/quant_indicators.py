"""
Deterministic technical indicators, computed straight from the candles
table. No LLM involved here on purpose - this is fast, free, and exact.

Supports Parquet backend for 10-100x faster reads when available.
Supports Polars for 2-5x faster indicator computation when available.
"""
import pandas as pd
import numpy as np
from storage.db import get_connection

# Try to import parquet store
try:
    from storage.parquet_store import load_candles_parquet, PARQUET_AVAILABLE
    _HAS_PARQUET = PARQUET_AVAILABLE
except ImportError:
    _HAS_PARQUET = False

# Try to import Polars for accelerated computation
try:
    import polars as pl
    _HAS_POLARS = True
except ImportError:
    _HAS_POLARS = False


def load_candles(symbol: str, limit: int = 200, use_parquet: bool = True) -> pd.DataFrame:
    """
    Load candles from fastest available source.
    
    Args:
        symbol: Stock symbol
        limit: Number of candles to load (from most recent)
        use_parquet: If True, try Parquet first (much faster for large datasets)
    """
    # Try Parquet first for speed
    if use_parquet and _HAS_PARQUET:
        try:
            df = load_candles_parquet(symbol, limit=limit)
            if not df.empty:
                return df
        except Exception:
            pass  # Fall back to SQLite
    
    # Fallback to SQLite
    conn = get_connection()
    df = pd.read_sql_query(
        "SELECT * FROM candles WHERE symbol = ? ORDER BY timestamp DESC LIMIT ?",
        conn, params=(symbol, limit),
    )
    conn.close()
    return df.iloc[::-1].reset_index(drop=True)  # chronological order


def load_candles_polars(symbol: str, limit: int = 200) -> "pl.DataFrame":
    """Load candles as Polars DataFrame for accelerated computation."""
    if not _HAS_POLARS:
        raise RuntimeError("Polars not installed. Run: pip install polars")
    
    if _HAS_PARQUET:
        try:
            from storage.parquet_store import symbol_parquet_dir
            parquet_file = symbol_parquet_dir(symbol, "minute") / "data.parquet"
            if parquet_file.exists():
                df = pl.read_parquet(str(parquet_file))
                if limit:
                    df = df.tail(limit)
                return df.sort("timestamp")
        except Exception:
            pass
    
    # Fallback to SQLite
    conn = get_connection()
    pdf = pd.read_sql_query(
        "SELECT * FROM candles WHERE symbol = ? ORDER BY timestamp DESC LIMIT ?",
        conn, params=(symbol, limit),
    )
    conn.close()
    return pl.from_pandas(pdf.iloc[::-1].reset_index(drop=True))


# --- Polars-accelerated indicators ---
# These are drop-in replacements that work with Polars DataFrames
# and return Polars Series for 2-5x speedup on large datasets

def compute_rsi_pl(df: "pl.DataFrame", period: int = 14) -> "pl.Series":
    """Polars-accelerated RSI computation."""
    if not _HAS_POLARS:
        raise RuntimeError("Polars not available")
    
    close = df["close"]
    delta = close.diff()
    
    gain = delta.clip(lower_bound=0)
    loss = (-delta).clip(lower_bound=0)
    
    # EWMA alpha = 1/period
    alpha = 1.0 / period
    
    # Polars ewm_mean
    avg_gain = gain.ewm_mean(alpha=alpha, adjust=False)
    avg_loss = loss.ewm_mean(alpha=alpha, adjust=False)
    
    rs = avg_gain / avg_loss.replace(0, 1e-9)
    return 100 - (100 / (1 + rs))


def compute_atr_pl(df: "pl.DataFrame", period: int = 14) -> "pl.Series":
    """Polars-accelerated ATR computation."""
    if not _HAS_POLARS:
        raise RuntimeError("Polars not available")
    
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    
    true_range = pl.max_horizontal(tr1, tr2, tr3)
    return true_range.rolling_mean(window_size=period)


def compute_adx_pl(df: "pl.DataFrame", period: int = 14) -> "pl.Series":
    """Polars-accelerated ADX computation."""
    if not _HAS_POLARS:
        raise RuntimeError("Polars not available")
    
    high = df["high"]
    low = df["low"]
    close = df["close"]
    
    up_move = high.diff()
    down_move = -low.diff()
    
    plus_dm = pl.when((up_move > down_move) & (up_move > 0)).then(up_move).otherwise(0)
    minus_dm = pl.when((down_move > up_move) & (down_move > 0)).then(down_move).otherwise(0)
    
    atr = compute_atr_pl(df, period).replace(0, 1e-9)
    
    alpha = 1.0 / period
    plus_di = 100 * plus_dm.ewm_mean(alpha=alpha, adjust=False) / atr
    minus_di = 100 * minus_dm.ewm_mean(alpha=alpha, adjust=False) / atr
    
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-9)
    return dx.ewm_mean(alpha=alpha, adjust=False)


def compute_macd_pl(df: "pl.DataFrame", fast: int = 12, slow: int = 26, signal: int = 9) -> tuple:
    """Polars-accelerated MACD computation."""
    if not _HAS_POLARS:
        raise RuntimeError("Polars not available")
    
    close = df["close"]
    ema_fast = close.ewm_mean(span=fast, adjust=False)
    ema_slow = close.ewm_mean(span=slow, adjust=False)
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm_mean(span=signal, adjust=False)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def indicators_from_df_pl(df: "pl.DataFrame") -> dict:
    """Polars version of indicators_from_df - computes all indicators at once."""
    if not _HAS_POLARS:
        raise RuntimeError("Polars not available")
    
    if len(df) < 20:
        return {"error": "not enough candles yet"}
    
    # Compute all indicators in parallel using Polars expressions
    df = df.with_columns([
        compute_rsi_pl(df).alias("rsi"),
        compute_atr_pl(df).alias("atr"),
        compute_adx_pl(df).alias("adx"),
    ])
    
    last = df.tail(1).to_dicts()[0]
    
    return {
        "close": last["close"],
        "high": last["high"],
        "low": last["low"],
        "rsi": round(last["rsi"], 2) if last["rsi"] is not None and not np.isnan(last["rsi"]) else None,
        "atr": round(last["atr"], 2) if last["atr"] is not None and not np.isnan(last["atr"]) else None,
        "adx": round(last["adx"], 2) if last["adx"] is not None and not np.isnan(last["adx"]) else None,
        "timestamp": last["timestamp"],
    }


def latest_indicators_pl(symbol: str) -> dict:
    """Polars-accelerated latest_indicators."""
    df = load_candles_polars(symbol, limit=200)
    if len(df) < 20:
        return {"symbol": symbol, "error": "not enough candles yet"}
    result = indicators_from_df_pl(df)
    result["symbol"] = symbol
    return result


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
        # The bar's own extremes, unrounded. A stop is a resting order: it
        # fills on the first trade through the level, not on the bar's close,
        # so anything judging a stop (risk/position_monitor.py) has to read
        # low/high rather than close. Kept raw (not rounded) so the comparison
        # is against the price that actually traded.
        "high": last["high"],
        "low": last["low"],
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
