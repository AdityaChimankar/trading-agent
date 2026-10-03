"""
Strategy Optimization Loop - Iterates through parameter combinations and
scaling configurations to find the best performing setup for 500+ symbols.

Runs a series of backtests/walk-forward optimizations with different:
- Parameter grids (adx_threshold, atr multipliers, filters)
- Symbol subsets (liquidity-filtered, full watchlist)
- Data intervals (minute, 5minute)
- Lookback windows (30, 60, 120 days)

Outputs the best configuration with statistical validation.

Auto-detects system resources (CPU cores, RAM) and configures parallel
workers to maximize throughput without OOM or throttling.
"""
import sys
import time
import json
import itertools
import os
from dataclasses import dataclass, asdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

from core.signals import (
    prepare_symbol_series, simulate_trades, DEFAULT_PARAMS,
    LOOKBACK_MIN, MAX_HOLD_CANDLES, SLIPPAGE,
    verify_fast_path_matches_reference
)
from research.backtest import run_multi_backtest, get_all_watchlist_symbols, summarize
from research.walk_forward_optimizer import run_walk_forward_multi, PARAM_GRID, TRAIN_CANDLES, TEST_CANDLES
from storage.db import get_connection, get_watchlist_symbols
from core.quant_indicators import load_candles, compute_atr
from core.config import get_config


def detect_optimal_workers(reserve_cores: int = 2, reserve_ram_gb: float = 4.0) -> int:
    """
    Detect optimal worker count based on system resources.
    
    Args:
        reserve_cores: Physical cores to leave free for OS/other apps
        reserve_ram_gb: GB of RAM to reserve for OS/other apps
    
    Returns:
        Optimal number of worker processes
    """
    if not PSUTIL_AVAILABLE:
        return min(4, os.cpu_count() or 4)
    
    # CPU-based limit: use physical cores minus reserve
    physical_cores = psutil.cpu_count(logical=False) or psutil.cpu_count(logical=True) or 4
    cpu_workers = max(1, physical_cores - reserve_cores)
    
    # Memory-based limit: estimate ~500MB per worker for backtest (candles + indicators)
    avail_ram_gb = psutil.virtual_memory().available / (1024**3)
    ram_workers = max(1, int((avail_ram_gb - reserve_ram_gb) / 0.5))
    
    # Cap at logical cores
    logical_cores = psutil.cpu_count(logical=True) or physical_cores * 2
    optimal = min(cpu_workers, ram_workers, logical_cores)
    
    print(f"System: {physical_cores}P/{logical_cores}L cores, {psutil.virtual_memory().total/(1024**3):.1f}GB RAM")
    print(f"  Available RAM: {avail_ram_gb:.1f}GB -> RAM limit: {ram_workers} workers")
    print(f"  CPU limit: {cpu_workers} workers (reserve {reserve_cores} cores)")
    print(f"  Optimal workers: {optimal}")
    
    return optimal


def get_system_info() -> dict:
    """Get detailed system information for logging."""
    info = {
        "cpu_physical": os.cpu_count(),
        "cpu_logical": os.cpu_count(),
        "ram_total_gb": 0,
        "ram_available_gb": 0,
    }
    if PSUTIL_AVAILABLE:
        info.update({
            "cpu_physical": psutil.cpu_count(logical=False),
            "cpu_logical": psutil.cpu_count(logical=True),
            "ram_total_gb": round(psutil.virtual_memory().total / (1024**3), 1),
            "ram_available_gb": round(psutil.virtual_memory().available / (1024**3), 1),
        })
    return info


# Global optimal worker count (set once at startup)
OPTIMAL_WORKERS = detect_optimal_workers()
SYSTEM_INFO = get_system_info()


@dataclass
class OptimizationResult:
    config_name: str
    params: dict
    symbols_tested: int
    total_trades: int
    win_rate: float
    avg_return_pct: float
    sharpe_like: float
    max_drawdown_pct: float
    total_return_pct: float
    elapsed_seconds: float
    timestamp: str


@dataclass
class SymbolMetrics:
    symbol: str
    avg_volume: float
    avg_atr_pct: float
    candle_count: int
    liquidity_score: float
    # Enhanced metrics
    avg_daily_volume: float = 0.0
    volume_stability: float = 0.0  # Coefficient of variation (lower = more stable)
    price_stability: float = 0.0   # Coefficient of variation of returns
    avg_spread_bps: float = 0.0    # Estimated spread in basis points


def compute_symbol_metrics(symbol: str, days: int = 60) -> Optional[SymbolMetrics]:
    """Compute liquidity/volatility metrics for a symbol to enable pre-filtering."""
    df = load_candles(symbol, limit=375 * days)  # ~375 minute candles/day
    if len(df) < 100:
        return None

    avg_volume = df["volume"].mean()
    atr = compute_atr(df).iloc[-1]
    close = df["close"].iloc[-1]
    avg_atr_pct = (atr / close) if close > 0 and not pd.isna(atr) else 0
    candle_count = len(df)

    # Daily volume (resample to daily)
    df_ts = df.copy()
    df_ts["ts"] = pd.to_datetime(df_ts["timestamp"], format="ISO8601")
    df_ts = df_ts.set_index("ts")
    daily_vol = df_ts["volume"].resample("D").sum()
    avg_daily_volume = daily_vol.mean() if len(daily_vol) > 0 else avg_volume * 375
    volume_stability = daily_vol.std() / daily_vol.mean() if daily_vol.mean() > 0 else 1.0

    # Price stability (daily returns CV)
    daily_returns = df_ts["close"].resample("D").last().pct_change().dropna()
    price_stability = daily_returns.std() / abs(daily_returns.mean()) if abs(daily_returns.mean()) > 1e-6 else 1.0

    # Estimated spread from ATR%
    avg_spread_bps = avg_atr_pct * 10000  # ATR% -> basis points

    # Liquidity score: volume * inverse spread * stability factors
    liquidity_score = (
        avg_daily_volume * 
        (1 / max(avg_atr_pct, 1e-6)) * 
        (1 / max(volume_stability, 0.1)) * 
        (1 / max(price_stability, 0.1))
    ) if avg_atr_pct > 0 else 0

    return SymbolMetrics(
        symbol=symbol,
        avg_volume=float(avg_volume),
        avg_atr_pct=float(avg_atr_pct),
        candle_count=candle_count,
        liquidity_score=float(liquidity_score),
        avg_daily_volume=float(avg_daily_volume),
        volume_stability=float(volume_stability),
        price_stability=float(price_stability),
        avg_spread_bps=float(avg_spread_bps),
    )


def get_liquid_symbols(
    top_n: int = 100, 
    min_candles: int = 5000, 
    days: int = 60,
    min_avg_daily_volume: float = 1e6,  # 1M shares/day minimum
    max_spread_bps: float = 200,        # Max 2% spread
    use_cache: bool = True
) -> list:
    """
    Return top N symbols by liquidity score from watchlist.
    
    Filters:
    - Minimum candles for statistical significance
    - Minimum average daily volume
    - Maximum estimated spread
    """
    from paths import PROJECT_ROOT
    cache_path = PROJECT_ROOT / "data" / "liquidity_cache.json"
    
    # Try cache first
    if use_cache and cache_path.exists():
        try:
            with open(cache_path) as f:
                cached = json.load(f)
            # Check cache age (< 1 day)
            cache_time = pd.Timestamp(cached.get("timestamp", "2000-01-01"))
            if pd.Timestamp.now() - cache_time < pd.Timedelta(days=1):
                cached_symbols = cached.get("symbols", [])
                if cached_symbols:
                    print(f"Using cached liquidity ranking ({len(cached_symbols)} symbols)")
                    return cached_symbols[:top_n]
        except Exception:
            pass
    
    all_symbols = get_watchlist_symbols()
    print(f"Computing liquidity metrics for {len(all_symbols)} symbols...")

    metrics = []
    with ProcessPoolExecutor(max_workers=OPTIMAL_WORKERS) as executor:
        futures = {executor.submit(compute_symbol_metrics, s, days): s for s in all_symbols}
        for future in as_completed(futures):
            result = future.result()
            if result and result.candle_count >= min_candles:
                metrics.append(result)

    # Apply filters
    filtered = [
        m for m in metrics 
        if m.avg_daily_volume >= min_avg_daily_volume 
        and m.avg_spread_bps <= max_spread_bps
    ]
    
    filtered.sort(key=lambda m: m.liquidity_score, reverse=True)
    selected = [m.symbol for m in filtered[:top_n]]
    
    # Save cache
    if use_cache:
        cache_path.parent.mkdir(exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump({
                "timestamp": pd.Timestamp.now().isoformat(),
                "symbols": [m.symbol for m in filtered],
                "metrics": {m.symbol: asdict(m) for m in filtered}
            }, f)
    
    print(f"Selected top {len(selected)} liquid symbols (min {min_candles} candles, "
          f"min daily vol {min_avg_daily_volume:,.0f}, max spread {max_spread_bps}bps)")
    return selected


def build_param_grids() -> list:
    """Generate parameter grids to test - focused on high-impact parameters."""
    grids = []

    # Grid 1: Conservative (wider stops, higher ADX)
    grids.append({
        "name": "conservative",
        "params": {
            "adx_threshold": [25, 30],
            "atr_sl_mult": [2.0, 2.5],
            "atr_tp_mult": [4.0, 5.0, 6.0],
            "trail_mult": [2.0, 3.0],
            "use_trailing": [True],
            "use_take_profit": [False],
            "min_atr_pct": [0.0005, 0.0008],
            "rsi_max_long": [55, 60],
            "rsi_min_short": [40, 45],
            "volume_mult": [1.2, 1.5],
        }
    })

    # Grid 2: Aggressive (tighter stops, lower ADX)
    grids.append({
        "name": "aggressive",
        "params": {
            "adx_threshold": [15, 20],
            "atr_sl_mult": [1.0, 1.5],
            "atr_tp_mult": [3.0, 4.0],
            "trail_mult": [1.0, 1.5],
            "use_trailing": [True, False],
            "use_take_profit": [False],
            "min_atr_pct": [0.0003, 0.0005],
            "rsi_max_long": [60, 65],
            "rsi_min_short": [35, 40],
            "volume_mult": [1.0, 1.2],
        }
    })

    # Grid 3: Balanced (current defaults vicinity)
    grids.append({
        "name": "balanced",
        "params": {
            "adx_threshold": [20, 25],
            "atr_sl_mult": [1.5, 2.0],
            "atr_tp_mult": [4.0, 5.0],
            "trail_mult": [1.5, 2.0],
            "use_trailing": [False],
            "use_take_profit": [False],
            "min_atr_pct": [0.0005],
            "rsi_max_long": [60],
            "rsi_min_short": [40],
            "volume_mult": [1.2],
        }
    })

    # Grid 4: Exit-focused (validating stop-only vs stop+target)
    grids.append({
        "name": "exit_focused",
        "params": {
            "adx_threshold": [20],
            "atr_sl_mult": [1.5, 2.0],
            "atr_tp_mult": [4.0, 5.0],
            "trail_mult": [2.0],
            "use_trailing": [False],
            "use_take_profit": [True, False],  # Key comparison
            "min_atr_pct": [0.0005],
            "rsi_max_long": [60],
            "rsi_min_short": [40],
            "volume_mult": [1.2],
        }
    })

    return grids


def expand_grid(grid: dict) -> list:
    """Expand parameter grid into list of dict combinations."""
    keys = list(grid["params"].keys())
    values = list(grid["params"].values())
    combos = list(itertools.product(*values))
    return [dict(zip(keys, combo)) for combo in combos]


def check_memory_safety(min_available_gb: float = 2.0) -> bool:
    """Check if enough memory is available to continue safely."""
    if not PSUTIL_AVAILABLE:
        return True
    avail_gb = psutil.virtual_memory().available / (1024**3)
    if avail_gb < min_available_gb:
        print(f"  WARNING: Low memory ({avail_gb:.1f}GB available), pausing...")
        return False
    return True


def wait_for_memory(min_available_gb: float = 2.0, check_interval: int = 5, max_wait: int = 300):
    """Wait until sufficient memory is available."""
    if not PSUTIL_AVAILABLE:
        return
    waited = 0
    while waited < max_wait:
        avail_gb = psutil.virtual_memory().available / (1024**3)
        if avail_gb >= min_available_gb:
            return
        print(f"  Waiting for memory... ({avail_gb:.1f}GB available, need {min_available_gb}GB)")
        time.sleep(check_interval)
        waited += check_interval
    print(f"  WARNING: Memory still low after {max_wait}s, continuing anyway")


def run_backtest_config(symbols: list, params: dict, config_name: str) -> OptimizationResult:
    """Run a single backtest configuration and return results."""
    # Memory safety check
    wait_for_memory()
    
    start = time.time()

    # Merge with defaults
    test_params = {**DEFAULT_PARAMS, **params}

    # Run backtest
    result = run_multi_backtest(symbols, parallel=True, max_workers=OPTIMAL_WORKERS)

    elapsed = time.time() - start

    return OptimizationResult(
        config_name=config_name,
        params=test_params,
        symbols_tested=len(symbols),
        total_trades=result.get("trades", 0),
        win_rate=result.get("win_rate", 0),
        avg_return_pct=result.get("avg_win_pct", 0) * result.get("win_rate", 0) / 100
                        + result.get("avg_loss_pct", 0) * (100 - result.get("win_rate", 0)) / 100,
        sharpe_like=result.get("sharpe_like_ratio", 0),
        max_drawdown_pct=result.get("max_drawdown_pct", 0),
        total_return_pct=result.get("total_return_pct", 0),
        elapsed_seconds=elapsed,
        timestamp=pd.Timestamp.now().isoformat()
    )


def run_walkforward_config(symbols: list, params: dict, config_name: str) -> OptimizationResult:
    """Run walk-forward optimization for a config."""
    start = time.time()

    # Temporarily override PARAM_GRID
    import research.walk_forward_optimizer as wfo
    original_grid = wfo.PARAM_GRID
    wfo.PARAM_GRID = {k: [v] if not isinstance(v, list) else v for k, v in params.items()}

    try:
        result = run_walk_forward_multi(symbols, parallel=True, max_workers=OPTIMAL_WORKERS)
        elapsed = time.time() - start

        # Aggregate per-symbol results
        total_trades = sum(r["optimized"].get("trades", 0) for r in result["per_symbol"].values())
        total_return = sum(r["optimized"].get("total_return_pct", 0) for r in result["per_symbol"].values())
        avg_win_rate = np.mean([r["optimized"].get("win_rate", 0) for r in result["per_symbol"].values()])

        return OptimizationResult(
            config_name=config_name,
            params=params,
            symbols_tested=len(result["per_symbol"]),
            total_trades=total_trades,
            win_rate=avg_win_rate,
            avg_return_pct=total_return / max(len(result["per_symbol"]), 1),
            sharpe_like=0,  # Not computed in walk-forward summary
            max_drawdown_pct=0,
            total_return_pct=total_return,
            elapsed_seconds=elapsed,
            timestamp=pd.Timestamp.now().isoformat()
        )
    finally:
        wfo.PARAM_GRID = original_grid


def score_result(r: OptimizationResult, min_trades: int = 100) -> float:
    """Composite score: penalize low trade count, reward risk-adjusted return."""
    if r.total_trades < min_trades:
        return -1e6  # Heavy penalty for insufficient trades

    # Risk-adjusted return with drawdown penalty
    return_score = r.total_return_pct
    dd_penalty = abs(r.max_drawdown_pct) * 0.5
    trade_bonus = min(r.total_trades / 500, 2.0) * 5  # Bonus up to +10 for 500+ trades

    return return_score - dd_penalty + trade_bonus


def save_results(results: list, output_path: Path):
    """Save optimization results to JSON."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data = [asdict(r) for r in results]
    with open(output_path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Saved {len(results)} results to {output_path}")


def load_results(input_path: Path) -> list:
    """Load optimization results from JSON."""
    with open(input_path) as f:
        data = json.load(f)
    return [OptimizationResult(**d) for d in data]


def print_summary(results: list, top_n: int = 5):
    """Print top configurations."""
    scored = [(score_result(r), r) for r in results]
    scored.sort(key=lambda x: x[0], reverse=True)

    print(f"\n{'='*80}")
    print(f"TOP {top_n} CONFIGURATIONS")
    print(f"{'='*80}")
    for i, (score, r) in enumerate(scored[:top_n], 1):
        print(f"\n#{i} Score: {score:.2f} | {r.config_name}")
        print(f"    Trades: {r.total_trades} | Win%: {r.win_rate:.1f} | Return: {r.total_return_pct:.2f}% | DD: {r.max_drawdown_pct:.2f}% | Sharpe: {r.sharpe_like:.3f}")
        print(f"    Symbols: {r.symbols_tested} | Time: {r.elapsed_seconds:.0f}s")
        for k, v in r.params.items():
            if k in ["adx_threshold", "atr_sl_mult", "atr_tp_mult", "use_trailing", "use_take_profit", "min_atr_pct"]:
                print(f"      {k}: {v}")


def optimization_loop(
    symbol_sets: dict = None,
    param_grids: list = None,
    mode: str = "backtest",  # "backtest" or "walkforward"
    output_dir: str = "optimization_results",
    min_trades: int = 100,
    workers: int = None  # Override auto-detected workers
):
    """
    Main optimization loop.

    Args:
        symbol_sets: Dict of {name: [symbols]} to test different symbol subsets
        param_grids: List of parameter grid configs from build_param_grids()
        mode: "backtest" for simple backtest, "walkforward" for walk-forward optimization
        output_dir: Directory to save results
        min_trades: Minimum trades required for a valid result
        workers: Override worker count (default: auto-detected)
    """
    global OPTIMAL_WORKERS
    if workers is not None:
        OPTIMAL_WORKERS = workers
        print(f"Worker override: {workers}")
    
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # Save system info for reproducibility
    system_info_path = output_path / "system_info.json"
    with open(system_info_path, "w") as f:
        json.dump({**SYSTEM_INFO, "workers_used": OPTIMAL_WORKERS}, f, indent=2)

    # Default symbol sets
    if symbol_sets is None:
        all_symbols = get_watchlist_symbols()
        symbol_sets = {
            "full_watchlist": all_symbols,
            "top_50_liquid": get_liquid_symbols(50),
            "top_100_liquid": get_liquid_symbols(100),
            "top_200_liquid": get_liquid_symbols(200),
        }

    # Default param grids
    if param_grids is None:
        param_grids = build_param_grids()

    all_results = []

    # Run optimization for each symbol set × param grid combination
    for set_name, symbols in symbol_sets.items():
        if not symbols:
            print(f"Skipping {set_name}: no symbols")
            continue

        print(f"\n{'='*60}")
        print(f"SYMBOL SET: {set_name} ({len(symbols)} symbols)")
        print(f"{'='*60}")

        for grid in param_grids:
            combos = expand_grid(grid)
            print(f"\n  Grid: {grid['name']} ({len(combos)} combinations)")

            for i, params in enumerate(combos):
                config_name = f"{set_name}_{grid['name']}_{i}"
                print(f"    [{i+1}/{len(combos)}] {config_name}...", end=" ", flush=True)

                try:
                    if mode == "walkforward":
                        result = run_walkforward_config(symbols, params, config_name)
                    else:
                        result = run_backtest_config(symbols, params, config_name)

                    all_results.append(result)
                    print(f"Done ({result.total_trades} trades, {result.total_return_pct:.2f}% return)")

                    # Save incrementally
                    save_results(all_results, output_path / f"results_{mode}.json")

                except Exception as e:
                    print(f"FAILED: {e}")
                    continue

    # Final summary
    print_summary(all_results, top_n=10)

    # Save best config
    best = max(all_results, key=score_result, default=None)
    if best:
        best_path = output_path / f"best_config_{mode}.json"
        with open(best_path, "w") as f:
            json.dump(asdict(best), f, indent=2)
        print(f"\nBest config saved to {best_path}")
        print(f"Best: {best.config_name} | Score: {score_result(best, min_trades):.2f}")

    return all_results


def run_selftest_all(symbols: list = None) -> bool:
    """Run self-test on all symbols to verify fast path matches reference."""
    if symbols is None:
        symbols = get_watchlist_symbols()[:20]  # Sample for speed

    print(f"Running self-test on {len(symbols)} symbols...")
    all_passed = True
    for symbol in symbols:
        matches = verify_fast_path_matches_reference(symbol)
        status = "PASS" if matches else "FAIL"
        print(f"  {symbol}: {status}")
        if not matches:
            all_passed = False

    print(f"\nSelf-test: {'PASSED' if all_passed else 'FAILED'}")
    return all_passed


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Strategy Optimization Loop")
    parser.add_argument("--mode", choices=["backtest", "walkforward"], default="backtest")
    parser.add_argument("--symbol-set", choices=["full", "top50", "top100", "top200"], default="top100")
    parser.add_argument("--grid", choices=["conservative", "aggressive", "balanced", "exit_focused", "all"], default="all")
    parser.add_argument("--output", default="optimization_results")
    parser.add_argument("--min-trades", type=int, default=100)
    parser.add_argument("--workers", type=int, default=None, help="Override auto-detected worker count")
    parser.add_argument("--selftest", action="store_true", help="Run self-test only")
    args = parser.parse_args()

    if args.selftest:
        run_selftest_all()
        sys.exit(0)

    # Build symbol sets
    all_symbols = get_watchlist_symbols()
    symbol_sets = {
        "full": all_symbols,
        "top50": get_liquid_symbols(50),
        "top100": get_liquid_symbols(100),
        "top200": get_liquid_symbols(200),
    }
    selected_sets = {args.symbol_set: symbol_sets[args.symbol_set]}

    # Build param grids
    all_grids = build_param_grids()
    if args.grid != "all":
        selected_grids = [g for g in all_grids if g["name"] == args.grid]
    else:
        selected_grids = all_grids

    print(f"Starting optimization: mode={args.mode}, symbols={args.symbol_set}, grids={[g['name'] for g in selected_grids]}")

    optimization_loop(
        symbol_sets=selected_sets,
        param_grids=selected_grids,
        mode=args.mode,
        output_dir=args.output,
        min_trades=args.min_trades,
        workers=args.workers
    )