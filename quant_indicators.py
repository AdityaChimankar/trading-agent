"""
Deterministic technical indicators, computed straight from the candles
table. No LLM involved here on purpose - this is fast, free, and exact.
"""
import pandas as pd
from db import get_connection


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
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = -delta.clip(upper=0).rolling(period).mean()
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
    plus_di = 100 * plus_dm.rolling(period).mean() / atr
    minus_di = 100 * minus_dm.rolling(period).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-9)
    return dx.rolling(period).mean()


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
