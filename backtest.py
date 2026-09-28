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
import bisect
import numpy as np
import pandas as pd
from db import get_connection
from quant_indicators import load_candles, compute_rsi, compute_atr, compute_adx
from pattern_detection import compute_pattern_columns, compute_swing_extrema, swing_bias_at
from decision_agent import evaluate_signals

LOOKBACK_MIN = 20        # min candles needed before indicators are valid
FORWARD_WINDOW = 6       # candles to look ahead for outcome (6 x 5min = 30 min hold)
SWING_ORDER = 5          # bars on each side required to confirm a swing high/low
NEUTRAL_SENTIMENT = 0.0  # see limitation note above

# Round-trip cost (slippage + charges) deducted from every simulated
# trade, as a fraction. Defined ONCE here and imported everywhere else
# that models a trade's return (walk_forward_optimizer.py, ml_features.py)
# so the labelling/training convention can never silently drift away
# from what the backtest actually measures.
SLIPPAGE = 0.0005

PATTERN_COLS_BULLISH = {"hammer", "bullish_engulfing", "double_bottom"}
PATTERN_COLS_BEARISH = {"bearish_engulfing", "double_top"}

# evaluate_signals()'s own defaults - held here (not imported) so
# vectorized_evaluate() below can apply them without a function-call per
# candle. Must match decision_agent.py's evaluate_signals() defaults
# exactly. This is the SINGLE definition for the whole project -
# walk_forward_optimizer.py imports it rather than keeping its own copy,
# because two copies of the same thresholds is exactly how a silent
# behavioural drift starts.
DEFAULT_PARAMS = {
    "rsi_oversold": 30, "rsi_overbought": 70, "adx_threshold": 20,
    "pattern_confirm_buy_rsi": 45, "pattern_confirm_sell_rsi": 55,
}


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
        "closes": df["close"].values,
        "opens": df["open"].values,
        "rsis": df["rsi"].values, "adxs": df["adx"].values,
        "dojis": df["doji"].values, "hammers": df["hammer"].values,
        "bull_engulf": df["bullish_engulfing"].values, "bear_engulf": df["bearish_engulfing"].values,
        "timestamps": df["timestamp"].values, "swings": swings,
    }


def precompute_pattern_bias_codes(prepared: dict, order: int = SWING_ORDER, tolerance: float = 0.015) -> np.ndarray:
    """Returns an int8 array (0=neutral, 1=bullish, 2=bearish) for every
    candle - a vectorized-friendly equivalent of calling
    pattern_detection.swing_bias_at() + the bullish/bearish set lookup
    at every index. Replicates the EXACT precedence those use:
      - single-candle: bearish_engulfing set first, then hammer/
        bullish_engulfing overwrite it (bullish wins on the same candle)
      - swing: double_top is checked BEFORE double_bottom with an early
        return (swing_bias_at) - so double_top always wins when both
        hold at the same point
      - overall: bullish wins if ANY bullish signal is present
    Verified byte-identical to the original per-candle loop across 135
    test cases (5 market conditions x 27 parameter combos) before this
    was trusted - see verify_fast_path_matches_reference() below to
    re-run that check on your own data.
    """
    n = prepared["length"]
    hammers, bull_engulf, bear_engulf = prepared["hammers"], prepared["bull_engulf"], prepared["bear_engulf"]
    swings = prepared["swings"]

    high_idxs = [j for j, _ in swings["highs"]]
    high_prices = [p for _, p in swings["highs"]]
    low_idxs = [j for j, _ in swings["lows"]]
    low_prices = [p for _, p in swings["lows"]]

    codes = np.zeros(n, dtype=np.int8)
    for i in range(n):
        cutoff = i - order
        swing = None
        h_count = bisect.bisect_right(high_idxs, cutoff)
        if h_count >= 2:
            h1, h2 = high_prices[h_count - 2], high_prices[h_count - 1]
            if abs(h1 - h2) / h1 < tolerance:
                swing = "double_top"
        if swing is None:
            l_count = bisect.bisect_right(low_idxs, cutoff)
            if l_count >= 2:
                l1, l2 = low_prices[l_count - 2], low_prices[l_count - 1]
                if abs(l1 - l2) / l1 < tolerance:
                    swing = "double_bottom"

        bullish = bool(hammers[i]) or bool(bull_engulf[i]) or (swing == "double_bottom")
        bearish = bool(bear_engulf[i]) or (swing == "double_top")
        if bullish:
            codes[i] = 1
        elif bearish:
            codes[i] = 2

    return codes


def vectorized_evaluate(rsi: np.ndarray, adx: np.ndarray, pattern_codes: np.ndarray, params: dict = None) -> np.ndarray:
    """Returns an int8 action array (0=HOLD, 1=BUY, 2=SELL) - vectorized
    equivalent of evaluate_signals()'s if/elif chain. ONLY valid at
    NEUTRAL_SENTIMENT (0.0), which is all backtest.py ever uses: the
    sentiment>0.3 / sentiment<-0.3 branches in evaluate_signals() are
    always False at sentiment=0.0, so only the pattern-confirmed
    branches can ever fire - which is exactly what makes BUY/SELL
    mutually exclusive by construction below."""
    p = {**DEFAULT_PARAMS, **(params or {})}

    with np.errstate(invalid="ignore"):
        adx_ok = adx >= p["adx_threshold"]
        buy = adx_ok & (pattern_codes == 1) & (rsi < p["pattern_confirm_buy_rsi"])
        sell = adx_ok & (pattern_codes == 2) & (rsi > p["pattern_confirm_sell_rsi"])

    actions = np.zeros(len(rsi), dtype=np.int8)
    actions[sell] = 2
    actions[buy] = 1
    actions[np.isnan(rsi) | np.isnan(adx)] = 0
    return actions


def _simulate_trades_reference(prepared: dict, params: dict = None, index_range: tuple = None) -> list:
    """SLOW REFERENCE IMPLEMENTATION - per-candle Python loop, kept only
    for correctness verification (see verify_fast_path_matches_reference()
    below), not used in the normal run path. This was the only
    implementation until it was found to take ~1.66s/symbol at ~19,000
    candles (~14 minutes projected serially across 500 symbols) - the
    vectorized simulate_trades() below replaces it as the default,
    verified to produce identical results before being trusted."""
    params = params or {}
    symbol = prepared["symbol"]
    closes, opens, rsis, adxs = prepared["closes"], prepared["opens"], prepared["rsis"], prepared["adxs"]
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

        # Enter at the NEXT candle's open (not the signal candle's close)
        # and exit FORWARD_WINDOW candles later, minus slippage - this
        # must match simulate_trades() exactly, or the selftest that
        # compares the two will (correctly) report a mismatch.
        entry_price, exit_price = opens[i + 1], closes[i + FORWARD_WINDOW]
        pct_return = (exit_price - entry_price) / entry_price
        if action == "SELL":
            pct_return = -pct_return
        pct_return -= SLIPPAGE

        trades.append({
            "symbol": symbol, "timestamp": timestamps[i], "action": action,
            "entry": entry_price, "exit": exit_price, "return_pct": pct_return,
        })

    return trades


def simulate_trades(prepared: dict, params: dict = None, index_range: tuple = None) -> list:
    """Vectorized decision loop - runs the same logic as
    _simulate_trades_reference() (and, by extension, evaluate_signals())
    but as NumPy array operations instead of a per-candle Python loop.
    ~16x faster at ~19,000 candles - see verify_fast_path_matches_reference()
    to independently confirm this on your own data before trusting it
    at scale."""
    start = max(LOOKBACK_MIN, index_range[0]) if index_range else LOOKBACK_MIN
    end = min(prepared["length"] - FORWARD_WINDOW, index_range[1]) if index_range else prepared["length"] - FORWARD_WINDOW
    if start >= end:
        return []

    pattern_codes = precompute_pattern_bias_codes(prepared)
    rsi_slice = prepared["rsis"][start:end]
    adx_slice = prepared["adxs"][start:end]
    pattern_slice = pattern_codes[start:end]
    actions = vectorized_evaluate(rsi_slice, adx_slice, pattern_slice, params)

    trade_idx = np.nonzero(actions != 0)[0]
    if len(trade_idx) == 0:
        return []

    closes, opens, timestamps, symbol = prepared["closes"], prepared["opens"], prepared["timestamps"], prepared["symbol"]
    trades = []
    for local_i in trade_idx:
        i = start + local_i
        
        # Enter at the NEXT candle's open, exit at the target candle's close
        entry_price, exit_price = opens[i + 1], closes[i + FORWARD_WINDOW]
        
        pct_return = (exit_price - entry_price) / entry_price
        if actions[local_i] == 2:
            pct_return = -pct_return
            
        # Deduct round-trip cost for slippage and transaction charges
        pct_return -= SLIPPAGE
        
        trades.append({
            "symbol": symbol, "timestamp": timestamps[i], # Keep original timestamp for the signal
            "action": "BUY" if actions[local_i] == 1 else "SELL",
            "entry": entry_price, "exit": exit_price, "return_pct": pct_return,
        })
    return trades


def verify_fast_path_matches_reference(symbol: str, params: dict = None) -> bool:
    """Independently confirms the vectorized simulate_trades() matches
    the slow per-candle reference implementation on YOUR actual data,
    not just the synthetic data this was checked against during
    development. Run via: python backtest.py --selftest [symbols]"""
    prepared = prepare_symbol_series(symbol)
    if prepared is None:
        return True  # nothing to check, not a mismatch
    index_range = (LOOKBACK_MIN, prepared["length"] - FORWARD_WINDOW)
    fast = simulate_trades(prepared, params=params, index_range=index_range)
    slow = _simulate_trades_reference(prepared, params=params, index_range=index_range)
    fast_key = [(t["timestamp"], t["action"], round(t["return_pct"], 10)) for t in fast]
    slow_key = [(t["timestamp"], t["action"], round(t["return_pct"], 10)) for t in slow]
    return fast_key == slow_key





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
    """
    IMPORTANT about total_return_pct: this is the SUM of individual
    trade returns (as if each trade used a fixed, separate, non-
    reinvested amount of capital), NOT a compounded portfolio return.
    Sequential 100%-reinvestment compounding only makes sense if trades
    never overlap in time and never span more than one symbol - neither
    holds for run_multi_backtest's pooled, multi-symbol trade lists.
    Naive compounding at trade counts in the hundreds of thousands
    produces absurd numbers (e.g. 10^23%) that look like a miracle
    strategy but are actually a broken metric, not a good one - this
    was caught exactly that way during development. The additive sum
    stays honest and bounded at any trade count or overlap pattern;
    read it as "expectancy x number of trades", not portfolio growth.
    """
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

    # Additive cumulative curve for drawdown - stable and interpretable
    # at any trade count, unlike a compounded (cumprod) curve.
    cumulative_additive = returns.cumsum()
    running_max = cumulative_additive.cummax()
    drawdown = cumulative_additive - running_max
    max_drawdown = drawdown.min()

    result = {
        "label": label, "trades": len(trades),
        "win_rate": round(win_rate * 100, 1),
        "avg_win_pct": round(avg_win * 100, 3),
        "avg_loss_pct": round(avg_loss * 100, 3),
        "risk_reward_ratio": round(risk_reward, 2),
        "sharpe_like_ratio": round(sharpe, 3),
        "max_drawdown_pct": round(max_drawdown * 100, 2),
        "total_return_pct": round(returns.sum() * 100, 2),
    }

    print(f"\n--- Backtest results: {label} ---")
    for k, v in result.items():
        print(f"  {k}: {v}")
    return result


MAX_WORKERS = 4  # capped, not os.cpu_count() - see walk_forward_optimizer.py's
                  # note on Windows page-file exhaustion from too many
                  # scipy-loading processes starting at once. Raise this
                  # only if you've also increased your system's virtual
                  # memory, or lower it if you still hit that error.


def run_multi_backtest(symbols: list, parallel: bool = True) -> dict:
    from concurrent.futures import ProcessPoolExecutor, as_completed

    print(f"Running backtest on {len(symbols)} symbol(s)"
          + (f" in parallel (max {MAX_WORKERS} workers)..." if parallel and len(symbols) > 1 else " serially..."))
    start = time.time()
    all_trades = []

    if parallel and len(symbols) > 1:
        with ProcessPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(collect_trades, s): s for s in symbols}
            done = 0
            for future in as_completed(futures):
                symbol = futures[future]
                done += 1
                try:
                    all_trades.extend(future.result())
                except Exception as e:
                    print(f"  [{done}/{len(symbols)}] {symbol}: FAILED - {e}")
                    continue
                if done % 25 == 0 or done == len(symbols):
                    elapsed = time.time() - start
                    eta = elapsed / done * (len(symbols) - done)
                    print(f"  Progress: {done}/{len(symbols)} symbols done "
                          f"({elapsed:.0f}s elapsed, ~{eta:.0f}s remaining)")
    else:
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


def run_selftest(symbols: list) -> None:
    """Runs verify_fast_path_matches_reference() across real symbols -
    lets you independently confirm the vectorized fast path matches
    the slow per-candle reference on YOUR actual market data, not just
    the synthetic data this was checked against during development."""
    all_passed = True
    for symbol in symbols:
        matches = verify_fast_path_matches_reference(symbol)
        print(f"  {symbol}: {'MATCH' if matches else 'MISMATCH'}")
        if not matches:
            all_passed = False
    print("\nSELFTEST PASSED - fast path matches the reference implementation on this data" if all_passed
          else "\nSELFTEST FAILED - do not trust backtest results until this is investigated")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        symbols = ["RELIANCE"]
    elif args[0] == "--all":
        symbols = get_all_watchlist_symbols()
    elif args[0] == "--selftest":
        run_selftest(args[1:] if len(args) > 1 else get_all_watchlist_symbols()[:5])
        sys.exit(0)
    else:
        symbols = args

    run_multi_backtest(symbols)
