"""
15-min Donchian Channel Breakout Strategy with ATR Stops
Trend-following: buy 20-period high breakout, sell 20-period low breakdown
Risk management: 2*ATR stop loss, 4*ATR take profit (1:2 R:R)
"""
import pandas as pd
import numpy as np
from core.quant_indicators import load_candles, compute_atr
from storage.db import get_connection


def prepare_15m_data(symbol: str, candle_limit: int = 20000) -> pd.DataFrame | None:
    """Load and resample to 15-min, compute Donchian channels and ATR."""
    df = load_candles(symbol, limit=candle_limit)
    if len(df) < 100:
        return None

    df_ts = df.copy()
    df_ts["ts"] = pd.to_datetime(df_ts["timestamp"], format="ISO8601")
    df_ts = df_ts.set_index("ts")
    df_15m = df_ts.resample("15min").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna()

    if len(df_15m) < 50:
        return None

    # Donchian channels (20-period)
    df_15m["high_20"] = df_15m["high"].rolling(20).max()
    df_15m["low_20"] = df_15m["low"].rolling(20).min()
    df_15m["atr"] = compute_atr(df_15m, period=14)

    # Breakout signals (close crosses above/below prior 20-period high/low)
    df_15m["buy_signal"] = (df_15m["close"] > df_15m["high_20"].shift(1)) & (df_15m["high_20"].notna())
    df_15m["sell_signal"] = (df_15m["close"] < df_15m["low_20"].shift(1)) & (df_15m["low_20"].notna())

    return df_15m


def simulate_donchian(symbol: str, sl_mult: float = 2.0, tp_mult: float = 4.0, max_hold: int = 10) -> list:
    """Simulate Donchian breakout with ATR-based stops."""
    df = prepare_15m_data(symbol)
    if df is None:
        return []

    trades = []
    for i in range(20, len(df) - max_hold):
        atr = df["atr"].iloc[i]
        if np.isnan(atr) or atr <= 0:
            continue

        # Long breakout
        if df["buy_signal"].iloc[i]:
            entry = df["open"].iloc[i + 1]
            sl = entry - sl_mult * atr
            tp = entry + tp_mult * atr

            for j in range(i + 1, min(i + 1 + max_hold, len(df))):
                if df["low"].iloc[j] <= sl:
                    ret = (sl - entry) / entry
                    trades.append({"symbol": symbol, "entry_time": df.index[i], "action": "BUY", "entry": entry, "exit": sl, "return_pct": ret, "exit_reason": "SL"})
                    break
                elif df["high"].iloc[j] >= tp:
                    ret = (tp - entry) / entry
                    trades.append({"symbol": symbol, "entry_time": df.index[i], "action": "BUY", "entry": entry, "exit": tp, "return_pct": ret, "exit_reason": "TP"})
                    break
            else:
                exit_p = df["close"].iloc[min(i + max_hold, len(df) - 1)]
                ret = (exit_p - entry) / entry
                trades.append({"symbol": symbol, "entry_time": df.index[i], "action": "BUY", "entry": entry, "exit": exit_p, "return_pct": ret, "exit_reason": "TIME"})

        # Short breakdown
        elif df["sell_signal"].iloc[i]:
            entry = df["open"].iloc[i + 1]
            sl = entry + sl_mult * atr
            tp = entry - tp_mult * atr

            for j in range(i + 1, min(i + 1 + max_hold, len(df))):
                if df["high"].iloc[j] >= sl:
                    ret = (entry - sl) / entry
                    trades.append({"symbol": symbol, "entry_time": df.index[i], "action": "SELL", "entry": entry, "exit": sl, "return_pct": ret, "exit_reason": "SL"})
                    break
                elif df["low"].iloc[j] <= tp:
                    ret = (entry - tp) / entry
                    trades.append({"symbol": symbol, "entry_time": df.index[i], "action": "SELL", "entry": entry, "exit": tp, "return_pct": ret, "exit_reason": "TP"})
                    break
            else:
                exit_p = df["close"].iloc[min(i + max_hold, len(df) - 1)]
                ret = (entry - exit_p) / entry
                trades.append({"symbol": symbol, "entry_time": df.index[i], "action": "SELL", "entry": entry, "exit": exit_p, "return_pct": ret, "exit_reason": "TIME"})

    return trades


def run_backtest(symbols: list, sl_mult: float = 2.0, tp_mult: float = 4.0) -> dict:
    """Run backtest on multiple symbols."""
    all_trades = []
    for sym in symbols:
        trades = simulate_donchian(sym, sl_mult, tp_mult)
        all_trades.extend(trades)
        if trades:
            rets = [t["return_pct"] for t in trades]
            wr = sum(1 for r in rets if r > 0) / len(rets)
            total = sum(rets) * 100
            print(f"{sym}: {len(trades)} trades, WR={wr:.1%}, total={total:.1f}%")

    if not all_trades:
        return {}

    rets = pd.Series([t["return_pct"] for t in all_trades])
    wins = rets[rets > 0]
    losses = rets[rets <= 0]
    return {
        "trades": len(all_trades),
        "win_rate": len(wins) / len(rets),
        "avg_win": wins.mean() if len(wins) else 0,
        "avg_loss": losses.mean() if len(losses) else 0,
        "risk_reward": abs(wins.mean() / losses.mean()) if len(losses) and losses.mean() != 0 else 0,
        "total_return_pct": rets.sum() * 100,
    }


if __name__ == "__main__":
    # Test on major symbols
    symbols = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN", "BHARTIARTL", "ITC", "KOTAKBANK", "AXISBANK", "LT", "MARUTI", "BAJFINANCE", "ASIANPAINT", "NESTLEIND", "SUNPHARMA", "DIVISLAB", "ULTRACEMCO", "ADANIPORTS", "ADANIENT", "ADANIGREEN", "ADANIPOWER", "TECHM", "TITAN", "ONGC", "BAJAJFINSV", "TATAMOTORS", "M&M", "HEROMOTOCO", "EICHERMOT", "TATACONSUM", "BAJAJ-AUTO", "WIPRO", "DRREDDY", "CIPLA", "GRASIM", "ADANIPORTS", "JSWSTEEL", "TATASTEEL", "COALINDIA", "NTPC", "POWERGRID"]
    result = run_backtest(symbols)
    print("\n=== COMBINED ===")
    for k, v in result.items():
        print(f"  {k}: {v}")