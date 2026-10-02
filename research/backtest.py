"""
Replays stored historical candles through the trend-following strategy
using simulate_trades() with ATR-based dynamic exits (stop loss, take profit,
trailing stop). Uses only data available up to each point (no lookahead).

To run across your full watchlist:
  python -m research.backtest RELIANCE TCS INFY ...
  python -m research.backtest --all

Run self-test to verify fast path matches reference:
  python -m research.backtest --selftest [symbols]
"""
import sys
import time
import pandas as pd
from core.signals import (
    LOOKBACK_MIN, MAX_HOLD_CANDLES, SLIPPAGE,
    prepare_symbol_series, simulate_trades, verify_fast_path_matches_reference,
    MAX_WORKERS,
)
from storage.db import get_connection


def collect_trades(symbol: str, candle_limit: int = 20000) -> list:
    """Runs the backtest loop for one symbol with DEFAULT parameters
    over its FULL history."""
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
    cumulative_additive = returns.cumsum()
    running_max = cumulative_additive.cummax()
    drawdown = cumulative_additive - running_max
    max_drawdown = drawdown.min()

    # Exit reason breakdown
    exit_reasons = {}
    for t in trades:
        reason = t.get("exit_reason", "UNKNOWN")
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1

    result = {
        "label": label, "trades": len(trades),
        "win_rate": round(win_rate * 100, 1),
        "avg_win_pct": round(avg_win * 100, 3),
        "avg_loss_pct": round(avg_loss * 100, 3),
        "risk_reward_ratio": round(risk_reward, 2),
        "sharpe_like_ratio": round(sharpe, 3),
        "max_drawdown_pct": round(max_drawdown * 100, 2),
        "total_return_pct": round(returns.sum() * 100, 2),
        "exit_reasons": exit_reasons,
    }

    print(f"\n--- Backtest results: {label} ---")
    for k, v in result.items():
        print(f"  {k}: {v}")
    return result


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
    the slow per-candle reference on YOUR actual market data."""
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