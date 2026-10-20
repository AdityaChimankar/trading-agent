"""Walk-forward optimizer for research."""

from __future__ import annotations

import bisect
import itertools
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict

import numpy as np
import pandas as pd

from trading_agent.core.logging_config import get_logger
from trading_agent.core.signals import (
    FORWARD_WINDOW,
    LOOKBACK_MIN,
    SWING_ORDER,
    DEFAULT_PARAMS,
)
from trading_agent.ingest.watchlist_resolved import get_watchlist_symbols

logger = get_logger(__name__)

TRAIN_CANDLES = 3750
TEST_CANDLES = 750
MIN_TRAIN_TRADES = 15
MAX_WORKERS = 4

PARAM_GRID = {
    "pattern_confirm_buy_rsi": [35, 45, 50],
    "pattern_confirm_sell_rsi": [50, 55, 65],
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
    p = {**asdict(DEFAULT_PARAMS), **(params or {})}
    with np.errstate(invalid="ignore"):
        adx_ok = adx >= p["adx_threshold"]
        buy = adx_ok & (pattern_codes == 1) & (rsi < p["pattern_confirm_buy_rsi"])
        sell = adx_ok & (pattern_codes == 2) & (rsi > p["pattern_confirm_sell_rsi"])
    actions = np.zeros(len(rsi), dtype=np.int8)
    actions[sell] = 2
    actions[buy] = 1
    actions[~np.isfinite(rsi) | ~np.isfinite(adx)] = 0
    return actions


def vectorized_trades(
    prepared: dict, pattern_codes: np.ndarray, params: dict | None, index_range: tuple,
    cost_bps: float = 5.0,
) -> list:
    start, end = index_range
    start = max(LOOKBACK_MIN, start)
    # An outcome must close strictly inside its own train/test window.
    end = min(prepared["length"], end) - FORWARD_WINDOW
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
    next_free = start
    for local_i in trade_idx:
        i = start + local_i
        if i < next_free:
            continue
        entry_price, exit_price = prepared["opens"][i + 1], closes[i + FORWARD_WINDOW]
        if not np.isfinite([entry_price, exit_price]).all() or min(entry_price, exit_price) <= 0:
            continue
        pct_return = (exit_price - entry_price) / entry_price
        if actions[local_i] == 2:
            pct_return = -pct_return
        pct_return -= cost_bps / 10000
        next_free = i + FORWARD_WINDOW
        trades.append(
            {
                "symbol": prepared["symbol"],
                "timestamp": timestamps[i],
                "signal_index": int(i),
                "entry_index": int(i + 1),
                "exit_index": int(i + FORWARD_WINDOW),
                "action": "BUY" if actions[local_i] == 1 else "SELL",
                "entry": entry_price,
                "exit": exit_price,
                "return_pct": pct_return,
            }
        )
    return trades


def vectorized_score(
    prepared: dict, pattern_codes: np.ndarray, params: dict, index_range: tuple,
    cost_bps: float = 5.0,
) -> tuple:
    trades = vectorized_trades(prepared, pattern_codes, params, index_range, cost_bps)
    count = len(trades)
    if count < MIN_TRAIN_TRADES:
        return float("-inf"), count
    return float(sum(trade["return_pct"] for trade in trades)), count


def optimize_fold(prepared: dict, pattern_codes: np.ndarray, train_range: tuple,
                  cost_bps: float = 5.0) -> dict | None:
    best_params, best_score = None, float("-inf")
    for params in _grid_combinations():
        score, _ = vectorized_score(prepared, pattern_codes, params, train_range, cost_bps)
        if score > best_score:
            best_score, best_params = score, params
    return best_params


def run_walk_forward(symbol: str, verbose: bool = True, *, interval: str = "minute",
                     train_candles: int = TRAIN_CANDLES, test_candles: int = TEST_CANDLES,
                     cost_bps: float = 5.0) -> dict:
    """Stored-candle, rule-only research. Never fit/promote the installed ML model."""
    from datetime import UTC, datetime
    from trading_agent.analysis.research_engine import completed_frame, INTERVAL_SECONDS
    from trading_agent.storage.parquet_store import get_parquet_store
    from trading_agent.strategy.research_features import prepare_research_frame

    if interval not in INTERVAL_SECONDS:
        raise ValueError("Unsupported interval")
    if not (250 <= train_candles <= 10000 and 100 <= test_candles <= 5000):
        raise ValueError("Train window must be 250-10000 and test window 100-5000 candles")
    if not np.isfinite(cost_bps) or not 0 <= cost_bps <= 100:
        raise ValueError("Round-trip cost must be 0-100 basis points")
    frame = completed_frame(get_parquet_store(), symbol, interval, datetime.now(UTC))
    required = train_candles + test_candles
    if len(frame) < required:
        return {"symbol": symbol, "interval": interval, "status": "insufficient_history",
                "candles": len(frame), "required_candles": required, "folds": []}
    prices = frame[["open", "high", "low", "close"]].to_numpy(dtype=float)
    if not np.isfinite(prices).all() or (prices <= 0).any():
        raise ValueError("Invalid OHLC prices in research history")
    df = prepare_research_frame(frame)
    prepared = {"symbol": symbol, "length": len(df), "rsis": df.rsi.to_numpy(),
                "adxs": df.adx.to_numpy(), "opens": df.open.to_numpy(),
                "closes": df.close.to_numpy(), "timestamps": df.timestamp.to_numpy()}
    result = evaluate_walk_forward(prepared, df.pattern_code.to_numpy(), train_candles,
                                   test_candles, cost_bps)
    result.update({"interval": interval, "candles": len(df), "required_candles": required,
                   "data_start": df.timestamp.iloc[0].isoformat(),
                   "data_end": df.timestamp.iloc[-1].isoformat()})
    if verbose:
        import json
        print(json.dumps(result, indent=2, default=str))
    return result


def evaluate_walk_forward(prepared, pattern_codes, train_candles=TRAIN_CANDLES,
                          test_candles=TEST_CANDLES, cost_bps=5.0):
    """Rolling train selection followed by disjoint, unseen test windows."""
    symbol = prepared["symbol"]

    n = prepared["length"]
    fold_size = train_candles + test_candles

    optimized_oos_trades, default_oos_trades, fold_params_log = [], [], []
    start, folds = 0, []
    while start + fold_size <= n:
        train_range = (start, start + train_candles)
        test_range = (start + train_candles, start + fold_size)
        fold = {"train_start": train_range[0], "train_end_exclusive": train_range[1],
                "test_start": test_range[0], "test_end_exclusive": test_range[1],
                "test_from": str(prepared["timestamps"][test_range[0]]),
                "test_to": str(prepared["timestamps"][test_range[1] - 1])}

        best_params = optimize_fold(prepared, pattern_codes, train_range, cost_bps)
        if best_params is None:
            folds.append({**fold, "status": "insufficient_training_trades", "params": None})
            start += test_candles
            continue

        oos_optimized = vectorized_trades(prepared, pattern_codes, best_params, test_range, cost_bps)
        oos_default = vectorized_trades(prepared, pattern_codes, None, test_range, cost_bps)

        optimized_oos_trades.extend(oos_optimized)
        default_oos_trades.extend(oos_default)
        fold_params_log.append(best_params)

        folds.append({**fold, "status": "evaluated", "params": best_params,
                      "optimized": _summarize_quiet(oos_optimized),
                      "default": _summarize_quiet(oos_default)})
        start += test_candles

    return {
        "symbol": symbol,
        "status": "completed" if fold_params_log else "insufficient_training_trades",
        "optimized": _summarize_quiet(optimized_oos_trades),
        "default": _summarize_quiet(default_oos_trades),
        "fold_params": fold_params_log,
        "folds": folds,
        "evaluated_folds": len(fold_params_log),
        "train_candles": train_candles, "test_candles": test_candles,
        "purge_candles": FORWARD_WINDOW, "round_trip_cost_bps": cost_bps,
        "grid_size": len(_grid_combinations()), "minimum_training_trades": MIN_TRAIN_TRADES,
        "scope": "Rules only; no ML training, historical news, model promotion or orders",
        "method": "Next-bar open entry, exit after 12 bars, non-overlapping positions, same costs in train/test",
        "limitations": "Additive trade returns are not portfolio returns. No session-boundary exits, stop/target simulation, liquidity, taxes or financing model. Repeated research can overfit; retain a separate final holdout.",
    }


def _summarize_quiet(trades: list) -> dict:
    if not trades:
        return {"trades": 0, "win_rate": None, "total_return_pct": 0.0,
                "mean_return_pct": None, "max_drawdown_pct": 0.0}
    returns = pd.Series([t["return_pct"] for t in trades])
    wins = returns[returns > 0]
    curve = np.r_[0.0, returns.cumsum().to_numpy()]
    return {
        "trades": len(trades),
        "win_rate": round(len(wins) / len(returns) * 100, 1),
        "total_return_pct": round(float(returns.sum()) * 100, 4),
        "mean_return_pct": round(float(returns.mean()) * 100, 4),
        "max_drawdown_pct": round(float((curve - np.maximum.accumulate(curve)).min()) * 100, 4),
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
        opt_trades = result.get("optimized", {}).get("trades", 0)
        def_trades = result.get("default", {}).get("trades", 0)
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
    import argparse
    import json
    parser = argparse.ArgumentParser(description="Offline rule-only walk-forward evaluation; no model promotion")
    parser.add_argument("symbols", nargs="*", default=[])
    parser.add_argument("--all", action="store_true", help="Explicitly evaluate the configured watchlist (expensive)")
    parser.add_argument("--interval", choices=["minute", "5minute", "15minute", "day"], default="minute")
    parser.add_argument("--train-candles", type=int, default=TRAIN_CANDLES)
    parser.add_argument("--test-candles", type=int, default=TEST_CANDLES)
    parser.add_argument("--cost-bps", type=float, default=5.0, help="Assumed total round-trip friction in basis points")
    args = parser.parse_args()
    selected = get_watchlist_symbols() if args.all else (args.symbols or ["RELIANCE"])
    for symbol in selected:
        result = run_walk_forward(symbol.strip().upper(), verbose=False, interval=args.interval,
                                  train_candles=args.train_candles, test_candles=args.test_candles,
                                  cost_bps=args.cost_bps)
        print(json.dumps(result, indent=2, default=str))
