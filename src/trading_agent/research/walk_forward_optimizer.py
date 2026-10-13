"""Walk-forward optimizer for research."""

from __future__ import annotations

import bisect
import itertools
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import pandas as pd

from trading_agent.core.logging_config import get_logger
from trading_agent.core.signals import (
    FORWARD_WINDOW,
    LOOKBACK_MIN,
    SWING_ORDER,
    DEFAULT_PARAMS,
    prepare_symbol_series,
    summarize,
)
from trading_agent.ingest.watchlist_resolved import get_watchlist_symbols

logger = get_logger(__name__)

TRAIN_CANDLES = 3750
TEST_CANDLES = 750
MIN_TRAIN_TRADES = 15
MAX_WORKERS = 4

PARAM_GRID = {
    "rsi_oversold": [25, 30, 35],
    "rsi_overbought": [65, 70, 75],
    "adx_threshold": [15, 20, 25],
}


def _grid_combinations() -> list:
    keys = list(PARAM_GRID.keys())
    return [
        dict(zip(keys, values, strict=False)) for values in itertools.product(*PARAM_GRID.values())
    ]


def precompute_pattern_bias_codes(
    prepared: dict, order: int = SWING_ORDER, tolerance: float = 0.015
) -> np.ndarray:
    n = prepared["length"]
    hammers, bull_engulf, bear_engulf = (
        prepared["hammers"],
        prepared["bull_engulf"],
        prepared["bear_engulf"],
    )
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


def vectorized_evaluate(
    rsi: np.ndarray, adx: np.ndarray, pattern_codes: np.ndarray, params: dict
) -> np.ndarray:
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


def vectorized_trades(
    prepared: dict, pattern_codes: np.ndarray, params: dict, index_range: tuple
) -> list:
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
        trades.append(
            {
                "symbol": prepared["symbol"],
                "timestamp": timestamps[i],
                "action": "BUY" if actions[local_i] == 1 else "SELL",
                "entry": entry_price,
                "exit": exit_price,
                "return_pct": pct_return,
            }
        )
    return trades


def vectorized_score(
    prepared: dict, pattern_codes: np.ndarray, params: dict, index_range: tuple
) -> tuple:
    start, end = index_range
    start = max(LOOKBACK_MIN, start)
    end = min(prepared["length"] - FORWARD_WINDOW, end)
    if start >= end:
        return float("-inf"), 0

    rsi_slice = prepared["rsis"][start:end]
    adx_slice = prepared["adxs"][start:end]
    pattern_slice = pattern_codes[start:end]
    actions = vectorized_evaluate(rsi_slice, adx_slice, pattern_slice, params)

    opens_entry = prepared["opens"][start + 1 : end + 1]
    closes_exit = prepared["closes"][start + FORWARD_WINDOW : end + FORWARD_WINDOW]

    pct_returns = (closes_exit - opens_entry) / opens_entry
    pct_returns = np.where(actions == 2, -pct_returns, pct_returns)
    pct_returns -= 0.0005

    mask = actions != 0
    count = int(mask.sum())
    if count < MIN_TRAIN_TRADES:
        return float("-inf"), count
    return float(pct_returns[mask].sum()), count


def optimize_fold(prepared: dict, pattern_codes: np.ndarray, train_range: tuple) -> dict | None:
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
            print(f"{symbol}: only {n} candles - need at least {fold_size}")
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
                print(
                    f"  Fold {len(fold_params_log) + 1}: no combo cleared {MIN_TRAIN_TRADES} min trades"
                )
            start += TEST_CANDLES
            continue

        oos_optimized = vectorized_trades(prepared, pattern_codes, best_params, test_range)
        oos_default = vectorized_trades(prepared, pattern_codes, None, test_range)

        optimized_oos_trades.extend(oos_optimized)
        default_oos_trades.extend(oos_default)
        fold_params_log.append(best_params)

        if verbose:
            print(
                f"  Fold {len(fold_params_log)}: train[{train_range[0]}:{train_range[1]}] "
                f"test[{test_range[0]}:{test_range[1]}] -> {best_params}, "
                f"{len(oos_optimized)} OOS trades (optimized) vs {len(oos_default)} (default)"
            )

        start += TEST_CANDLES

    if not fold_params_log:
        return {}

    optimized_result = (
        summarize(f"{symbol} - OPTIMIZED" if verbose else symbol, optimized_oos_trades)
        if verbose
        else _summarize_quiet(optimized_oos_trades)
    )
    default_result = (
        summarize(f"{symbol} - DEFAULT" if verbose else symbol, default_oos_trades)
        if verbose
        else _summarize_quiet(default_oos_trades)
    )

    return {
        "symbol": symbol,
        "optimized": optimized_result,
        "default": default_result,
        "fold_params": fold_params_log,
    }


def _summarize_quiet(trades: list) -> dict:
    if not trades:
        return {"trades": 0}
    returns = pd.Series([t["return_pct"] for t in trades])
    wins = returns[returns > 0]
    returns[returns <= 0]
    return {
        "trades": len(trades),
        "win_rate": round(len(wins) / len(returns) * 100, 1),
        "total_return_pct": round(((1 + returns).cumprod().iloc[-1] - 1) * 100, 2),
    }


def run_walk_forward_multi(symbols: list, parallel: bool = True) -> dict:
    print(
        f"Running walk-forward optimization on {len(symbols)} symbol(s)"
        + (f" in parallel (max {MAX_WORKERS} workers)..." if parallel else " serially...")
    )
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
                    print(
                        f"  Progress: {done}/{len(symbols)} symbols done "
                        f"({elapsed:.0f}s elapsed, ~{eta:.0f}s remaining)"
                    )
    else:
        for i, symbol in enumerate(symbols, 1):
            result = run_walk_forward(symbol, verbose=False)
            if result:
                per_symbol_results[symbol] = result
            if i % 25 == 0 or i == len(symbols):
                print(f"  Progress: {i}/{len(symbols)} symbols done")

    elapsed = time.time() - start_time
    print(f"\nCompleted {len(per_symbol_results)}/{len(symbols)} symbols in {elapsed:.1f}s")

    _all_optimized_trades, _all_default_trades = [], []
    helped_count, hurt_count = 0, 0
    for symbol, result in per_symbol_results.items():
        opt_trades = result["optimized"].get("trades", 0)
        def_trades = result["default"].get("trades", 0)
        if opt_trades > 0 and def_trades > 0:
            if result["optimized"]["total_return_pct"] > result["default"]["total_return_pct"]:
                helped_count += 1
            else:
                hurt_count += 1

    print(
        f"\nOptimization helped on {helped_count} symbols, did not help on {hurt_count} symbols "
        f"(out of {helped_count + hurt_count} with enough trades to compare)"
    )
    print("\nPer-symbol results in 'per_symbol' key")

    return {"per_symbol": per_symbol_results, "helped": helped_count, "hurt": hurt_count}


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        run_walk_forward("RELIANCE")
    elif args[0] == "--all":
        run_walk_forward_multi(get_watchlist_symbols())
    elif len(args) == 1:
        run_walk_forward(args[0])
    else:
        run_walk_forward_multi(args)
