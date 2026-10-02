"""
Walk-forward optimizer - re-fits trend-following strategy thresholds
per stock on a rolling TRAIN window, then tests the chosen parameters
on the immediately FOLLOWING window it has never seen (out-of-sample).
Slides forward and repeats.

WHY THIS MATTERS: picking the single best-performing parameters on
your WHOLE historical dataset and reporting that performance is not
a real backtest - it's curve-fitting, because you're allowed to see
the future when choosing parameters. Walk-forward avoids this: every
out-of-sample result was scored using parameters chosen ONLY from
data before that window started.
"""
import sys
import time
import itertools
from concurrent.futures import ProcessPoolExecutor, as_completed
import numpy as np
import pandas as pd

from core.signals import (
    prepare_symbol_series, simulate_trades,
    DEFAULT_PARAMS, MAX_HOLD_CANDLES, LOOKBACK_MIN, SLIPPAGE,
)
from research.backtest import summarize
from storage.db import get_watchlist_symbols

TRAIN_CANDLES = 3750   # ~50 trading days of 5-min candles
TEST_CANDLES = 750     # ~10 trading days - out-of-sample window per fold
MIN_TRAIN_TRADES = 10  # skip a parameter combo on train if it fires too few trades to trust

MAX_WORKERS = 4

# Grid kept intentionally modest - a huge grid on a small train window
# risks picking noise, not a real pattern.
PARAM_GRID = {
    "adx_threshold": [20, 25, 30],
    "atr_sl_mult": [1.5, 2.0, 2.5],
    "atr_tp_mult": [3.0, 4.0, 5.0, 6.0],
    "trail_mult": [1.0, 1.5, 2.0, 3.0],
    "use_trailing": [True, False],
    "min_atr_pct": [0.0003, 0.0005, 0.0008],
}

def _grid_combinations() -> list:
    keys = list(PARAM_GRID.keys())
    return [dict(zip(keys, values)) for values in itertools.product(*PARAM_GRID.values())]


def score_trades(prepared: dict, params: dict, index_range: tuple) -> tuple:
    """Returns (total_return, trade_count) for a parameter combo on a range.
    Uses the full simulate_trades with dynamic exits for accurate scoring."""
    trades = simulate_trades(prepared, params=params, index_range=index_range)
    if not trades:
        return float("-inf"), 0
    returns = np.array([t["return_pct"] for t in trades])
    count = len(returns)
    if count < MIN_TRAIN_TRADES:
        return float("-inf"), count
    return float(returns.sum()), count


def optimize_fold(prepared: dict, train_range: tuple) -> dict:
    """Grid search on the TRAIN range only. Returns the best params found,
    or None if nothing cleared MIN_TRAIN_TRADES."""
    best_params, best_score = None, float("-inf")
    for params in _grid_combinations():
        score, _ = score_trades(prepared, params, train_range)
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

    optimized_oos_trades, default_oos_trades, fold_params_log = [], [], []
    start = 0
    while start + fold_size <= n:
        train_range = (start, start + TRAIN_CANDLES)
        test_range = (start + TRAIN_CANDLES, start + TRAIN_CANDLES + TEST_CANDLES)

        best_params = optimize_fold(prepared, train_range)
        if best_params is None:
            if verbose:
                print(f"  Fold {len(fold_params_log)+1}: no combo cleared {MIN_TRAIN_TRADES} min "
                      f"trades on train window - skipping")
            start += TEST_CANDLES
            continue

        oos_optimized = simulate_trades(prepared, params=best_params, index_range=test_range)
        oos_default = simulate_trades(prepared, params=None, index_range=test_range)

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
        "total_return_pct": round(returns.sum() * 100, 2),
    }


def run_walk_forward_multi(symbols: list, parallel: bool = True) -> dict:
    print(f"Running walk-forward optimization on {len(symbols)} symbol(s)"
          + (f" in parallel (max {MAX_WORKERS} workers)..." if parallel else " serially..."))
    start_time = time.time()
    per_symbol_results = {}

    if parallel and len(symbols) > 1:
        with ProcessPoolExecutor(max_workers=MAX_WORKERS) as executor:
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
    """Runs verify_fast_path_matches_loop() across real symbols and
    several parameter combos - lets you independently confirm the fast
    vectorized path matches the original per-candle loop on YOUR
    actual market data, not just the synthetic data this was
    originally verified against during development."""
    from core.signals import verify_fast_path_matches_reference
    all_passed = True
    for symbol in symbols:
        prepared = prepare_symbol_series(symbol)
        if prepared is None:
            continue
        test_range = (LOOKBACK_MIN, prepared["length"] - 2)

        symbol_passed = True
        for params in _grid_combinations():
            if not verify_fast_path_matches_reference(symbol, params):
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