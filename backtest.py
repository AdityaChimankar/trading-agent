"""
Replays stored historical candles through evaluate_signals() - the
same rule function the live agent uses - using only data available up
to each point (no lookahead). For each BUY/SELL signal, checks what
price actually did over the next N candles and scores the outcome.

PERFORMANCE NOTE: indicators and patterns are precomputed ONCE per
symbol (vectorized), not recomputed at every step. Recomputing RSI/
ATR/ADX/patterns from scratch at each of n steps is O(n) work done n
times = O(n^2) - fine for one stock, but crawls at 500 symbols x ~19k
candles/year each. Precomputing is O(n) total per symbol because RSI/
ATR/ADX/patterns only ever look backward, so a value computed once
over the full series is identical to recomputing it on a window ending
at that same point - just far cheaper.

To reach a statistically meaningful sample (500+ trades) or to scale
to hundreds of symbols, run across your full watchlist:
  python backtest.py RELIANCE TCS INFY ...
  python backtest.py --all

KNOWN LIMITATION: historical sentiment is set to neutral (0.0) here,
since news isn't reliably backfilled candle-by-candle the way price
is. This backtest validates the RSI/ADX/pattern logic in isolation.
"""
import sys
import time
import pandas as pd
from db import get_connection
from quant_indicators import load_candles, compute_rsi, compute_atr, compute_adx
from pattern_detection import compute_pattern_columns, compute_swing_extrema, swing_bias_at
from decision_agent import evaluate_signals

LOOKBACK_MIN = 20        # min candles needed before indicators are valid
FORWARD_WINDOW = 6       # candles to look ahead for outcome (6 x 5min = 30 min hold)
SWING_ORDER = 5          # bars on each side required to confirm a swing high/low
NEUTRAL_SENTIMENT = 0.0  # see limitation note above

PATTERN_COLS_BULLISH = {"hammer", "bullish_engulfing", "double_bottom"}
PATTERN_COLS_BEARISH = {"bearish_engulfing", "double_top"}


def prepare_symbol_series(symbol: str, candle_limit: int = 20000) -> dict | None:
    """Precomputes indicators/patterns ONCE for a symbol (vectorized) and
    returns the raw arrays. Separated from simulate_trades() so a grid
    search over many parameter combinations (walk_forward_optimizer.py)
    only pays this cost once per symbol, not once per combination."""
    df = load_candles(symbol, limit=candle_limit)
    if len(df) < LOOKBACK_MIN + FORWARD_WINDOW + 10:
        print(f"{symbol}: not enough history ({len(df)} candles) - skipping")
        return None

    df["rsi"] = compute_rsi(df)
    df["atr"] = compute_atr(df)
    df["adx"] = compute_adx(df)
    df = compute_pattern_columns(df)
    swings = compute_swing_extrema(df, order=SWING_ORDER)

    return {
        "symbol": symbol, "length": len(df),
        "closes": df["close"].values, "rsis": df["rsi"].values, "adxs": df["adx"].values,
        "dojis": df["doji"].values, "hammers": df["hammer"].values,
        "bull_engulf": df["bullish_engulfing"].values, "bear_engulf": df["bearish_engulfing"].values,
        "timestamps": df["timestamp"].values, "swings": swings,
    }


def simulate_trades(prepared: dict, params: dict = None, index_range: tuple = None) -> list:
    """Runs the decision loop using PRECOMPUTED arrays from
    prepare_symbol_series(), with given evaluate_signals() parameters
    (or defaults if params=None), restricted to index_range=(start, end)
    if given - lets the walk-forward optimizer evaluate the same
    precomputed series on different train/test slices without
    recomputing indicators for each slice or each parameter combination."""
    params = params or {}
    symbol = prepared["symbol"]
    closes, rsis, adxs = prepared["closes"], prepared["rsis"], prepared["adxs"]
    dojis, hammers = prepared["dojis"], prepared["hammers"]
    bull_engulf, bear_engulf = prepared["bull_engulf"], prepared["bear_engulf"]
    timestamps, swings = prepared["timestamps"], prepared["swings"]

    start = max(LOOKBACK_MIN, index_range[0]) if index_range else LOOKBACK_MIN
    end = min(prepared["length"] - FORWARD_WINDOW, index_range[1]) if index_range else prepared["length"] - FORWARD_WINDOW

    trades = []
    for i in range(start, end):
        rsi, adx = rsis[i], adxs[i]
        if pd.isna(rsi) or pd.isna(adx):
            continue

        patterns_found = []
        if dojis[i]:
            patterns_found.append("doji")
        if hammers[i]:
            patterns_found.append("hammer")
        if bull_engulf[i]:
            patterns_found.append("bullish_engulfing")
        if bear_engulf[i]:
            patterns_found.append("bearish_engulfing")
        swing = swing_bias_at(swings, i, SWING_ORDER)
        if swing:
            patterns_found.append(swing)

        pattern_bias = "neutral"
        if any(p in PATTERN_COLS_BULLISH for p in patterns_found):
            pattern_bias = "bullish"
        elif any(p in PATTERN_COLS_BEARISH for p in patterns_found):
            pattern_bias = "bearish"

        action, rationale = evaluate_signals(rsi, adx, NEUTRAL_SENTIMENT, pattern_bias, patterns_found, **params)
        if action == "HOLD":
            continue

        entry_price, exit_price = closes[i], closes[i + FORWARD_WINDOW]
        pct_return = (exit_price - entry_price) / entry_price
        if action == "SELL":
            pct_return = -pct_return

        trades.append({
            "symbol": symbol, "timestamp": timestamps[i], "action": action,
            "entry": entry_price, "exit": exit_price, "return_pct": pct_return,
        })

    return trades


def collect_trades(symbol: str, candle_limit: int = 20000) -> list:
    """Runs the backtest loop for one symbol with DEFAULT parameters
    over its FULL history - kept for backward compatibility with
    existing single/multi-symbol backtest.py usage. Internally now
    just prepare + simulate with no param overrides and no range limit."""
    prepared = prepare_symbol_series(symbol, candle_limit)
    if prepared is None:
        return []
    trades = simulate_trades(prepared)
    print(f"{symbol}: {len(trades)} signals generated from {prepared['length']} candles")
    return trades


def summarize(label: str, trades: list) -> dict:
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

    cumulative = (1 + returns).cumprod()
    running_max = cumulative.cummax()
    drawdown = (cumulative - running_max) / running_max
    max_drawdown = drawdown.min()

    result = {
        "label": label, "trades": len(trades),
        "win_rate": round(win_rate * 100, 1),
        "avg_win_pct": round(avg_win * 100, 3),
        "avg_loss_pct": round(avg_loss * 100, 3),
        "risk_reward_ratio": round(risk_reward, 2),
        "sharpe_like_ratio": round(sharpe, 3),
        "max_drawdown_pct": round(max_drawdown * 100, 2),
        "total_return_pct": round((cumulative.iloc[-1] - 1) * 100, 2),
    }

    print(f"\n--- Backtest results: {label} ---")
    for k, v in result.items():
        print(f"  {k}: {v}")
    return result


def run_multi_backtest(symbols: list) -> dict:
    all_trades = []
    start = time.time()
    for symbol in symbols:
        all_trades.extend(collect_trades(symbol))
    elapsed = time.time() - start
    print(f"\nProcessed {len(symbols)} symbol(s) in {elapsed:.1f}s")

    if len(all_trades) < 500:
        print(f"Note: only {len(all_trades)} total trades - below the 500 target. "
              f"Pull more history (fetch_historical.py, higher days_back) or add more symbols.")

    return summarize(f"COMBINED ({len(symbols)} symbols)", all_trades)


def get_all_watchlist_symbols() -> list:
    conn = get_connection()
    symbols = [r["symbol"] for r in conn.execute("SELECT symbol FROM watchlist").fetchall()]
    conn.close()
    return symbols


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        symbols = ["RELIANCE"]
    elif args[0] == "--all":
        symbols = get_all_watchlist_symbols()
    else:
        symbols = args

    run_multi_backtest(symbols)
