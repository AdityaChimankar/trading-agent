"""
Edge diagnostics for the rule-based signal.

backtest.py already tells you the strategy loses money. This answers the
DIFFERENT and more important question: is there any edge here to tune, or
is the premise broken? Those need opposite responses -
  - a real edge that costs erode  -> trade more selectively, cut costs
  - no edge at all                -> no threshold tuning can fix it
and tuning thresholds on a signal with no edge just curve-fits noise.

THE BENCHMARK THAT MATTERS: entering at a random candle. If the signals'
average forward return does not beat the unconditional average forward
return over the same horizon, the entry rule is adding nothing and no
amount of RSI/ADX tuning will help. Every "edge" below is measured
against that zero-effort baseline, and always GROSS of slippage (slippage
is a fixed additive drag, so subtracting it from both sides cancels out;
seeing gross numbers is what tells you whether there is anything there).

Also reports a t-statistic for each mean. With hundreds of thousands of
candles almost anything is "statistically significant", so read the
t-stat as "is this distinguishable from noise", and the mean as "would
it survive costs".

Usage:
  python -m research.diagnose_edge                 # 60 symbols from the watchlist
  python -m research.diagnose_edge --n 150
  python -m research.diagnose_edge --all
  python -m research.diagnose_edge RELIANCE TCS    # specific symbols
  python -m research.diagnose_edge --n 40 --exits  # also test the exit logic (slower)
"""
import sys
import numpy as np
import pandas as pd

from core.signals import (
    prepare_symbol_series, vectorized_evaluate,
    FORWARD_WINDOW, LOOKBACK_MIN, SLIPPAGE,
)
from core.quant_indicators import load_candles, compute_atr
from storage.db import get_watchlist_symbols

HORIZONS = (1, 2, 3, 6, 12)
MIN_CANDLES = 2000

# Tolerances for the swing double-top/bottom price match. The shipped
# default is 0.015 (1.5%), which the frequency measurement below shows
# leaves the gate ON for almost every candle.
TOLERANCES = (0.015, 0.010, 0.005, 0.003)


def _mean_t(x: np.ndarray) -> tuple:
    """(mean, t-statistic) of a 1-D sample. t = mean / standard error."""
    n = len(x)
    if n < 2:
        return float("nan"), float("nan")
    mean = float(x.mean())
    se = float(x.std(ddof=1)) / np.sqrt(n)
    return mean, (mean / se if se > 0 else float("nan"))


def analyse_symbol(symbol: str) -> dict | None:
    prepared = prepare_symbol_series(symbol)
    if prepared is None or prepared["length"] < MIN_CANDLES:
        return None

    n = prepared["length"]
    opens, closes = prepared["opens"], prepared["closes"]
    rsis, adxs = prepared["rsis"], prepared["adxs"]
    atrs = prepared["atrs"]
    volume_ratio = prepared["volume_ratio"]
    ema_fast = prepared["ema_fast"]
    ema_slow = prepared["ema_slow"]
    ema_pullback = prepared["ema_pullback"]
    htf_trend_up = prepared["htf_trend_up"]
    htf_trend_down = prepared["htf_trend_down"]

    # Everything below is aligned to the FORWARD_WINDOW horizon used by the
    # backtest: enter at the next candle's open, exit `W` candles later.
    lo, hi = LOOKBACK_MIN, n - FORWARD_WINDOW
    if hi - lo < 200:
        return None

    idx = np.arange(lo, hi)
    fwd = (closes[idx + FORWARD_WINDOW] - opens[idx + 1]) / opens[idx + 1]

    action = vectorized_evaluate(
        ema_fast[lo:hi], ema_slow[lo:hi], ema_pullback[lo:hi],
        htf_trend_up[lo:hi], htf_trend_down[lo:hi],
        adxs[lo:hi], atrs[lo:hi], closes[lo:hi],
        rsis[lo:hi], volume_ratio[lo:hi],
        None
    )

    # Horizon sweep: aggregate mean forward return for signal entries vs
    # every-candle entries, at several holding periods.
    horizon_stats = {}
    for h in HORIZONS:
        m = np.arange(lo, n - h)
        if len(m) < 200:
            continue
        fw = (closes[m + h] - opens[m + 1]) / opens[m + 1]
        act = vectorized_evaluate(
            ema_fast[m], ema_slow[m], ema_pullback[m],
            htf_trend_up[m], htf_trend_down[m],
            adxs[m], atrs[m], closes[m],
            rsis[m], volume_ratio[m],
            None
        )
        # Direction-adjusted: a SELL's return is the negative of the move.
        signed = np.where(act == 2, -fw, fw)
        trade = signed[act != 0]
        horizon_stats[h] = (trade, fw)

    # No pattern-gate tolerance sweep for EMA pullback strategy
    # (the old pattern-gate is replaced by EMA/HTF trend/ADX/RSI/volume filters)
    gate_stats = {}

    return {
        "symbol": symbol,
        "fwd": fwd, "action": action,
        "horizon_stats": horizon_stats, "gate_stats": gate_stats,
    }


def _line(label: str, mean: float, t: float, n: int, baseline: float) -> str:
    edge = mean - baseline
    return (f"  {label:<34} n={n:>7}  mean={mean * 100:+.4f}%  "
            f"edge={edge * 100:+.4f}%  t={t:+.1f}")


def analyse_exit_variants(symbol: str, max_bars: int = 24) -> dict | None:
    """Path-dependent exit comparison.

    WHY THIS EXISTS: backtest.py always exits at a FIXED 6 candles no
    matter what price does. The live agent does something completely
    different - a stop-loss at 1.5x ATR and a take-profit at 2:1
    reward:risk (position_sizing.py), with position_monitor.py closing on
    stop / target / signal-reversal. So every backtest number produced so
    far describes an exit the system never actually uses. This walks each
    signal forward bar-by-bar until a level is touched.

    Ties inside a single bar are resolved as the STOP filling first, which
    is the conservative assumption (we cannot know the intrabar path).
    """
    prepared = prepare_symbol_series(symbol)
    if prepared is None or prepared["length"] < MIN_CANDLES:
        return None

    n = prepared["length"]
    opens, closes = prepared["opens"], prepared["closes"]
    raw = load_candles(symbol, limit=20000)
    if len(raw) != n:
        return None
    highs, lows = raw["high"].values, raw["low"].values
    atrs = prepared["atrs"]

    # Use EMA pullback strategy signals
    lo, hi = LOOKBACK_MIN, n - FORWARD_WINDOW
    action = vectorized_evaluate(
        prepared["ema_fast"][lo:hi], prepared["ema_slow"][lo:hi], prepared["ema_pullback"][lo:hi],
        prepared["htf_trend_up"][lo:hi], prepared["htf_trend_down"][lo:hi],
        prepared["adxs"][lo:hi], prepared["atrs"][lo:hi], closes[lo:hi],
        prepared["rsis"][lo:hi], prepared["volume_ratio"][lo:hi],
        None
    )

    variants = (("fixed6", 0.0, False), ("stop_target", 1.5, True),
                ("stop_only", 1.5, False), ("triple_stop", 3.0, True))
    returns = {name: [] for name, _, _ in variants}
    outcomes = {"stop": 0, "target": 0, "timeout": 0}

    for local_i in np.nonzero(action != 0)[0]:
        i = lo + int(local_i)
        entry, a = opens[i + 1], atrs[i]
        if not np.isfinite(entry) or entry <= 0 or not np.isfinite(a) or a <= 0:
            continue
        is_buy = action[local_i] == 1
        sign = 1.0 if is_buy else -1.0

        for name, stop_mult, use_target in variants:
            if name == "fixed6":
                ret = sign * (closes[i + FORWARD_WINDOW] - entry) / entry
                returns[name].append(ret - SLIPPAGE)
                continue

            stop_dist = stop_mult * a
            target_dist = 2.0 * stop_dist
            stop = entry - sign * stop_dist
            target = entry + sign * target_dist

            outcome, ret = "timeout", None
            for b in range(i + 1, min(i + 1 + max_bars, n)):
                if is_buy:
                    hit_stop, hit_target = lows[b] <= stop, use_target and highs[b] >= target
                else:
                    hit_stop, hit_target = highs[b] >= stop, use_target and lows[b] <= target
                if hit_stop:
                    outcome, ret = "stop", -stop_dist / entry
                    break
                if hit_target:
                    outcome, ret = "target", target_dist / entry
                    break
            if ret is None:
                j = min(i + max_bars, n - 1)
                ret = sign * (closes[j] - entry) / entry

            returns[name].append(ret - SLIPPAGE)
            if name == "stop_target":
                outcomes[outcome] += 1

    return {"returns": {k: np.asarray(v) for k, v in returns.items() if len(v)}, "outcomes": outcomes}


def main(symbols: list, include_exits: bool = False) -> None:
    parts = []
    skipped = 0
    for i, symbol in enumerate(symbols, 1):
        result = analyse_symbol(symbol)
        if result is None:
            skipped += 1
            continue
        parts.append(result)
        if i % 25 == 0:
            print(f"  ...{i}/{len(symbols)} symbols scanned")

    if not parts:
        print("No usable symbols - run `python -m ingest.fetch_historical` first.")
        return

    print(f"\nAnalysed {len(parts)} symbols ({skipped} skipped for too little history)")

    fwd = np.concatenate([p["fwd"] for p in parts])
    action = np.concatenate([p["action"] for p in parts])

    baseline_long, baseline_long_t = _mean_t(fwd)
    baseline_short = -baseline_long

    print(f"\n{'=' * 78}")
    print("1. THE BASELINE - what entering at a RANDOM candle returns")
    print(f"{'=' * 78}")
    print(f"  {'unconditional long (any candle)':<34} n={len(fwd):>7}  "
          f"mean={baseline_long * 100:+.4f}%  t={baseline_long_t:+.1f}")
    print(f"  {'unconditional short (any candle)':<34} n={len(fwd):>7}  "
          f"mean={baseline_short * 100:+.4f}%  t={-baseline_long_t:+.1f}")
    print(f"  (net of slippage these become {baseline_long * 100 - SLIPPAGE * 100:+.4f}% / "
          f"{baseline_short * 100 - SLIPPAGE * 100:+.4f}%)")

    print(f"\n{'=' * 78}")
    print("2. WHAT THE SIGNALS ACTUALLY RETURN vs THAT BASELINE (gross)")
    print(f"{'=' * 78}")
    buy = action == 1
    sell = action == 2
    any_sig = action != 0
    print(_line("ALL signals (direction-adjusted)", *_mean_t(np.where(sell, -fwd, fwd)[any_sig]),
                int(any_sig.sum()), 0.0))
    print(_line("BUY signals", *_mean_t(fwd[buy]), int(buy.sum()), baseline_long))
    print(_line("SELL signals", *_mean_t(-fwd[sell]), int(sell.sum()), baseline_short))

    print(f"\n  Signal frequency: {any_sig.sum()} signals over {len(action)} candles "
          f"= {any_sig.mean() * 100:.1f}% of all candles fire a BUY or SELL.")
    print(f"  That is the first red flag if it is high - a signal that fires on a "
          f"quarter of all candles is not selecting anything.")

    print(f"\n{'=' * 78}")
    print("3. COMPONENT ABLATION - which part, if any, carries the signal?")
    print(f"{'=' * 78}")
    print("  (long-perspective forward return of each gate, vs unconditional long)")
    # We need to re-fetch the indicators for component ablation
    # For now, skip detailed component ablation since it requires re-fetching data
    print("  (Component ablation requires re-fetching prepared data - skipped)")

    print(f"\n{'=' * 78}")
    print("4. HORIZON SENSITIVITY - is 6 candles simply the wrong hold?")
    print(f"{'=' * 78}")
    print("  horizon  signal gross EV    baseline gross EV     edge       signal n")
    for h in HORIZONS:
        trades, fw_all = [], []
        for p in parts:
            if h in p["horizon_stats"]:
                tr, allr = p["horizon_stats"][h]
                trades.append(tr)
                fw_all.append(allr)
        if not trades:
            continue
        tr = np.concatenate(trades)
        al = np.concatenate(fw_all)
        if len(tr) < 2:
            continue
        print(f"  {h:>5}    {tr.mean() * 100:+.4f}%            {al.mean() * 100:+.4f}%"
              f"            {(tr.mean() - al.mean()) * 100:+.4f}%   {len(tr):>8}")

    if include_exits:
        print(f"\n{'=' * 78}")
        print("8. EXIT LOGIC - the exit the system actually trades has never been tested")
        print(f"{'=' * 78}")
        print("  backtest.py always exits after a FIXED 6 candles. The live agent uses a")
        print("  1.5x-ATR stop with a 2:1 target (position_sizing.py). Walking each signal")
        print("  forward bar-by-bar to whichever level is hit first:")

        pooled = {}
        outcomes = {"stop": 0, "target": 0, "timeout": 0}
        exit_used = 0
        for s in symbols[:40]:
            r = analyse_exit_variants(s)
            if not r:
                continue
            exit_used += 1
            for k, v in r["returns"].items():
                pooled.setdefault(k, []).append(v)
            for k in outcomes:
                outcomes[k] += r["outcomes"][k]

        if pooled:
            labels = {
                "fixed6": "fixed 6-candle (backtest)",
                "stop_target": "1.5xATR stop + 2:1 target",
                "stop_only": "1.5xATR stop, no target",
                "triple_stop": "3.0xATR stop + 2:1 target",
            }
            print(f"\n  {'variant':<30} {'trades':>8}  {'net EV/trade':>13}  {'t':>6}")
            for k in ("fixed6", "stop_target", "stop_only", "triple_stop"):
                if k not in pooled:
                    continue
                arr = np.concatenate(pooled[k])
                m, t = _mean_t(arr)
                tag = "  (live)" if k == "stop_target" else ""
                print(f"  {labels[k]:<30} {len(arr):>8}  {m * 100:>+12.4f}%  {t:>+6.1f}{tag}")
            total_out = sum(outcomes.values())
            if total_out:
                print(f"\n  Outcome mix for the live variant ({exit_used} symbols): "
                      f"stop {outcomes['stop'] / total_out * 100:.1f}% · "
                      f"target {outcomes['target'] / total_out * 100:.1f}% · "
                      f"timeout {outcomes['timeout'] / total_out * 100:.1f}%")

    print(f"\n{'=' * 78}")
    print("READING THIS")
    print(f"{'=' * 78}")
    print("  - Section 2 edge is the whole ballgame. If the signals' edge over the")
    print("    baseline is ~0, the entry rule has no predictive content and no")
    print("    threshold tuning can create it. Costs then guarantee a loss.")
    print("  - Section 3 shows which component (if any) is doing work. If no single")
    print("    gate beats the baseline, the combination won't either.")
    print("  - Section 5 matters because a gate that is ON most of the time cannot")
    print("    be filtering anything - it is a constant, not a condition.")
    print(f"  - Slippage applied by the backtest is {SLIPPAGE * 100:.2f}% per trade.")
    print("    Anything whose edge is smaller than that loses money by construction.")


if __name__ == "__main__":
    args = sys.argv[1:]
    include_exits = "--exits" in args
    args = [a for a in args if a != "--exits"]

    if not args:
        symbols = get_watchlist_symbols()[:60]
    elif args[0] == "--all":
        symbols = get_watchlist_symbols()
    elif args[0] == "--n":
        symbols = get_watchlist_symbols()[: int(args[1])]
    else:
        symbols = args
    main(symbols, include_exits=include_exits)
