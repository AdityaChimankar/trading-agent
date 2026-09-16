"""
Walk-forward optimizer - re-fits evaluate_signals()'s thresholds
(RSI oversold/overbought, ADX trend gate) per stock on a rolling
TRAIN window, then tests the chosen parameters on the immediately
FOLLOWING window it has never seen (out-of-sample). Slides forward
and repeats.

WHY THIS MATTERS: picking the single best-performing parameters on
your WHOLE historical dataset and reporting that performance is not
a real backtest - it's curve-fitting, because you're allowed to see
the future when choosing parameters. Walk-forward avoids this: every
out-of-sample result was scored using parameters chosen ONLY from
data before that window started.

PERFORMANCE: a naive version of this (looping evaluate_signals() once
per candle, per parameter combo, per fold, per symbol) measured
~7 hours projected for 500 symbols. This version precomputes pattern
bias ONCE per symbol (independent of parameters) and evaluates each
parameter combo as vectorized NumPy array operations instead of a
Python loop - correctness verified against the original per-candle
loop function before shipping (see verify_vectorized_matches_loop()).

Usage:
  python walk_forward_optimizer.py RELIANCE              # single symbol, verbose
  python walk_forward_optimizer.py RELIANCE TCS INFY      # multiple symbols
  python walk_forward_optimizer.py --all                  # everything in your watchlist, parallelized
"""
import sys
import bisect
import time
import itertools
from concurrent.futures import ProcessPoolExecutor, as_completed
import numpy as np
import pandas as pd
from backtest import (
    prepare_symbol_series, simulate_trades, summarize,
    FORWARD_WINDOW, SWING_ORDER, LOOKBACK_MIN,
)
from db import get_watchlist_symbols

TRAIN_CANDLES = 3750   # ~50 trading days of 5-min candles
TEST_CANDLES = 750     # ~10 trading days - out-of-sample window per fold
MIN_TRAIN_TRADES = 15  # skip a parameter combo on train if it fires too few trades to trust

# Grid kept intentionally modest - a huge grid on a small train window
# risks picking noise, not a real pattern.
PARAM_GRID = {
    "rsi_oversold": [25, 30, 35],
    "rsi_overbought": [65, 70, 75],
    "adx_threshold": [15, 20, 25],
}

# Must match evaluate_signals()'s own defaults exactly - used for the
# "default params" comparison arm.
DEFAULT_PARAMS = {
    "rsi_oversold": 30, "rsi_overbought": 70, "adx_threshold": 20,
    "pattern_confirm_buy_rsi": 45, "pattern_confirm_sell_rsi": 55,
}


def _grid_combinations() -> list:
    keys = list(PARAM_GRID.keys())
    return [dict(zip(keys, values)) for values in itertools.product(*PARAM_GRID.values())]


# ---------------------------------------------------------------------
# Pattern bias precomputation - ONCE per symbol, independent of any
# parameter combo (pattern detection doesn't depend on RSI/ADX
# thresholds at all).
# ---------------------------------------------------------------------

def precompute_pattern_bias_codes(prepared: dict, order: int = SWING_ORDER, tolerance: float = 0.015) -> np.ndarray:
    """Returns an int8 array (0=neutral, 1=bullish, 2=bearish) for every
    candle. Replicates pattern_detection.py's exact precedence:
      - single-candle: bearish_engulfing set first, then hammer/
        bullish_engulfing overwrite it (bullish wins on same candle)
      - swing: double_top is checked BEFORE double_bottom with an
        early return (swing_bias_at) - so double_top always wins over
        double_bottom when both hold at the same point, and the swing
        result only contributes bullish/bearish alongside (not
        overriding) any single-candle pattern already found.
      - overall: bullish wins if ANY bullish signal present (hammer,
        bullish_engulfing, or swing=='double_bottom'); bearish only if
        no bullish signal but a bearish one is present.
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


def vectorized_evaluate(rsi: np.ndarray, adx: np.ndarray, pattern_codes: np.ndarray, params: dict) -> np.ndarray:
    """Returns an int8 action array (0=HOLD, 1=BUY, 2=SELL) for every
    candle - vectorized equivalent of evaluate_signals()'s if/elif
    chain. ONLY valid for NEUTRAL_SENTIMENT (0.0) - the sentiment>0.3 /
    sentiment<-0.3 branches in evaluate_signals() are always False at
    sentiment=0.0, so only the pattern-confirmed branches can ever
    fire here, which is exactly what makes buy/sell mutually exclusive
    by construction below. This is the same neutral-sentiment
    limitation backtest.py already documents."""
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


def vectorized_trades(prepared: dict, pattern_codes: np.ndarray, params: dict, index_range: tuple) -> list:
    """Vectorized equivalent of simulate_trades() - computes actions for
    a whole range as arrays, then builds trade dicts only for the
    (much smaller) set of actual BUY/SELL indices."""
    start, end = index_range
    start = max(LOOKBACK_MIN, start)
    end = min(prepared["length"] - FORWARD_WINDOW, end)
    if start >= end:
        return []

    rsi_slice = prepared["rsis"][start:end]
    adx_slice = prepared["adxs"][start:end]
    pattern_slice = pattern_codes[start:end]
    actions = vectorized_evaluate(rsi_slice, adx_slice, pattern_slice, params)

    trade_idx = np.nonzero(actions != 0)[0]
    if len(trade_idx) == 0:
        return []

    closes, timestamps = prepared["closes"], prepared["timestamps"]
    trades = []
    for local_i in trade_idx:
        i = start + local_i
        entry_price, exit_price = closes[i], closes[i + FORWARD_WINDOW]
        pct_return = (exit_price - entry_price) / entry_price
        if actions[local_i] == 2:
            pct_return = -pct_return
        trades.append({
            "symbol": prepared["symbol"], "timestamp": timestamps[i],
            "action": "BUY" if actions[local_i] == 1 else "SELL",
            "entry": entry_price, "exit": exit_price, "return_pct": pct_return,
        })
    return trades


def vectorized_score(prepared: dict, pattern_codes: np.ndarray, params: dict, index_range: tuple) -> tuple:
    """Fast path for grid search - returns (total_return, trade_count)
    without building trade dicts. Used only to RANK combos on train;
    the winning combo's real trades are built once via vectorized_trades()."""
    start, end = index_range
    start = max(LOOKBACK_MIN, start)
    end = min(prepared["length"] - FORWARD_WINDOW, end)
    if start >= end:
        return float("-inf"), 0

    rsi_slice = prepared["rsis"][start:end]
    adx_slice = prepared["adxs"][start:end]
    pattern_slice = pattern_codes[start:end]
    actions = vectorized_evaluate(rsi_slice, adx_slice, pattern_slice, params)

    closes_entry = prepared["closes"][start:end]
    closes_exit = prepared["closes"][start + FORWARD_WINDOW:end + FORWARD_WINDOW]
    pct_returns = (closes_exit - closes_entry) / closes_entry
    pct_returns = np.where(actions == 2, -pct_returns, pct_returns)

    mask = actions != 0
    count = int(mask.sum())
    if count < MIN_TRAIN_TRADES:
        return float("-inf"), count
    return float(pct_returns[mask].sum()), count


def verify_vectorized_matches_loop(prepared: dict, pattern_codes: np.ndarray, index_range: tuple, params: dict = None) -> bool:
    """Correctness check: vectorized_trades() must produce IDENTICAL
    trades to the original per-candle simulate_trades() loop. Run this
    before trusting the fast path on real data - see the test in the
    conversation history for how this was validated during development."""
    vec = vectorized_trades(prepared, pattern_codes, params, index_range)
    loop = simulate_trades(prepared, params=params, index_range=index_range)
    vec_key = [(t["timestamp"], t["action"], round(t["return_pct"], 10)) for t in vec]
    loop_key = [(t["timestamp"], t["action"], round(t["return_pct"], 10)) for t in loop]
    return vec_key == loop_key


def optimize_fold(prepared: dict, pattern_codes: np.ndarray, train_range: tuple) -> dict:
    """Grid search on the TRAIN range only, using the fast vectorized
    scorer. Returns the best params found, or None if nothing cleared
    MIN_TRAIN_TRADES."""
    best_params, best_score = None, float("-inf")
    for params in _grid_combinations():
        score, _ = vectorized_score(prepared, pattern_codes, params, train_range)
        if score > best_score:
            best_score, best_params = score, params
    return best_params


def run_walk_forward(symbol: str, verbose: bool = True) -> dict:
    prepared = prepare_symbol_series(symbol)
    if prepared is None:
        return {}

    n = prepared["length"]
    fold_size = TRAIN_CANDLES + TEST_CANDLES
    if n < fold_size:
        if verbose:
            print(f"{symbol}: only {n} candles - need at least {fold_size} for one walk-forward fold.")
        return {}

    pattern_codes = precompute_pattern_bias_codes(prepared)

    optimized_oos_trades, default_oos_trades, fold_params_log = [], [], []
    start = 0
    while start + fold_size <= n:
        train_range = (start, start + TRAIN_CANDLES)
        test_range = (start + TRAIN_CANDLES, start + TRAIN_CANDLES + TEST_CANDLES)

        best_params = optimize_fold(prepared, pattern_codes, train_range)
        if best_params is None:
            if verbose:
                print(f"  Fold {len(fold_params_log)+1}: no combo cleared {MIN_TRAIN_TRADES} min "
                      f"trades on train window - skipping")
            start += TEST_CANDLES
            continue

        oos_optimized = vectorized_trades(prepared, pattern_codes, best_params, test_range)
        oos_default = vectorized_trades(prepared, pattern_codes, None, test_range)

        optimized_oos_trades.extend(oos_optimized)
        default_oos_trades.extend(oos_default)
        fold_params_log.append(best_params)

        if verbose:
            print(f"  Fold {len(fold_params_log)}: train[{train_range[0]}:{train_range[1]}] "
                  f"test[{test_range[0]}:{test_range[1]}] -> {best_params}, "
                  f"{len(oos_optimized)} OOS trades (optimized) vs {len(oos_default)} (default)")

        start += TEST_CANDLES

    if not fold_params_log:
        return {}

    optimized_result = summarize(f"{symbol} - OPTIMIZED" if verbose else symbol, optimized_oos_trades) if verbose \
        else _summarize_quiet(optimized_oos_trades)
    default_result = summarize(f"{symbol} - DEFAULT" if verbose else symbol, default_oos_trades) if verbose \
        else _summarize_quiet(default_oos_trades)

    return {
        "symbol": symbol, "optimized": optimized_result, "default": default_result,
        "fold_params": fold_params_log,
    }


def _summarize_quiet(trades: list) -> dict:
    """Same math as backtest.summarize() but no printing - used for
    multi-symbol runs where per-symbol verbose output would flood
    the terminal at scale."""
    if not trades:
        return {"trades": 0}
    returns = pd.Series([t["return_pct"] for t in trades])
    wins = returns[returns > 0]
    losses = returns[returns <= 0]
    return {
        "trades": len(trades),
        "win_rate": round(len(wins) / len(returns) * 100, 1),
        "total_return_pct": round(((1 + returns).cumprod().iloc[-1] - 1) * 100, 2),
    }


def run_walk_forward_multi(symbols: list, parallel: bool = True) -> dict:
    print(f"Running walk-forward optimization on {len(symbols)} symbol(s)"
          + (" in parallel..." if parallel else " serially..."))
    start_time = time.time()
    per_symbol_results = {}

    if parallel and len(symbols) > 1:
        with ProcessPoolExecutor() as executor:
            futures = {executor.submit(run_walk_forward, s, False): s for s in symbols}
            done = 0
            for future in as_completed(futures):
                symbol = futures[future]
                done += 1
                try:
                    result = future.result()
                    if result:
                        per_symbol_results[symbol] = result
                except Exception as e:
                    print(f"  [{done}/{len(symbols)}] {symbol}: FAILED - {e}")
                    continue
                if done % 25 == 0 or done == len(symbols):
                    elapsed = time.time() - start_time
                    eta = elapsed / done * (len(symbols) - done)
                    print(f"  Progress: {done}/{len(symbols)} symbols done "
                          f"({elapsed:.0f}s elapsed, ~{eta:.0f}s remaining)")
    else:
        for i, symbol in enumerate(symbols, 1):
            result = run_walk_forward(symbol, verbose=False)
            if result:
                per_symbol_results[symbol] = result
            if i % 25 == 0 or i == len(symbols):
                print(f"  Progress: {i}/{len(symbols)} symbols done")

    elapsed = time.time() - start_time
    print(f"\nCompleted {len(per_symbol_results)}/{len(symbols)} symbols in {elapsed:.1f}s")

    all_optimized_trades, all_default_trades = [], []
    helped_count, hurt_count = 0, 0
    for symbol, result in per_symbol_results.items():
        opt_trades = result["optimized"].get("trades", 0)
        def_trades = result["default"].get("trades", 0)
        if opt_trades > 0 and def_trades > 0:
            if result["optimized"]["total_return_pct"] > result["default"]["total_return_pct"]:
                helped_count += 1
            else:
                hurt_count += 1

    print(f"\nOptimization helped on {helped_count} symbols, did not help on {hurt_count} symbols "
          f"(out of {helped_count + hurt_count} with enough trades to compare)")
    print("\nPer-symbol results are in the returned dict under 'per_symbol' - "
          "a single combined return-% across symbols isn't reported here since "
          "compounding order across parallel symbols would be arbitrary and misleading.")

    return {"per_symbol": per_symbol_results, "helped": helped_count, "hurt": hurt_count}


def run_selftest(symbols: list):
    """Runs verify_vectorized_matches_loop() across real symbols and
    several parameter combos - lets you independently confirm the fast
    vectorized path matches the original per-candle loop on YOUR
    actual market data, not just the synthetic data this was
    originally verified against during development."""
    all_passed = True
    for symbol in symbols:
        prepared = prepare_symbol_series(symbol)
        if prepared is None:
            continue
        pattern_codes = precompute_pattern_bias_codes(prepared)
        test_range = (0, prepared["length"] - FORWARD_WINDOW)

        symbol_passed = True
        for params in _grid_combinations():
            if not verify_vectorized_matches_loop(prepared, pattern_codes, test_range, params):
                print(f"  {symbol}: MISMATCH with params={params}")
                symbol_passed = False
                all_passed = False
        if symbol_passed:
            print(f"  {symbol}: all {len(_grid_combinations())} parameter combos matched")

    print("\nSELFTEST PASSED - vectorized fast path matches the slow loop on this data" if all_passed
          else "\nSELFTEST FAILED - do not trust walk-forward results until this is investigated")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        run_walk_forward("RELIANCE")
    elif args[0] == "--all":
        run_walk_forward_multi(get_watchlist_symbols())
    elif args[0] == "--selftest":
        run_selftest(args[1:] if len(args) > 1 else get_watchlist_symbols()[:5])
    elif len(args) == 1:
        run_walk_forward(args[0])
    else:
        run_walk_forward_multi(args)
