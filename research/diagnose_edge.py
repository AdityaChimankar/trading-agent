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

from research.backtest import (
    prepare_symbol_series, precompute_pattern_bias_codes, vectorized_evaluate,
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
    pcode = precompute_pattern_bias_codes(prepared)

    # Everything below is aligned to the FORWARD_WINDOW horizon used by the
    # backtest: enter at the next candle's open, exit `W` candles later.
    lo, hi = LOOKBACK_MIN, n - FORWARD_WINDOW
    if hi - lo < 200:
        return None

    idx = np.arange(lo, hi)
    fwd = (closes[idx + FORWARD_WINDOW] - opens[idx + 1]) / opens[idx + 1]
    rsi, adx = rsis[lo:hi], adxs[lo:hi]
    codes = pcode[lo:hi]
    action = vectorized_evaluate(rsi, adx, codes, None)

    single_bull = np.asarray(prepared["hammers"][lo:hi], dtype=bool) | np.asarray(
        prepared["bull_engulf"][lo:hi], dtype=bool
    )
    single_bear = np.asarray(prepared["bear_engulf"][lo:hi], dtype=bool)

    # Horizon sweep: aggregate mean forward return for signal entries vs
    # every-candle entries, at several holding periods.
    horizon_stats = {}
    for h in HORIZONS:
        m = np.arange(lo, n - h)
        if len(m) < 200:
            continue
        fw = (closes[m + h] - opens[m + 1]) / opens[m + 1]
        act = vectorized_evaluate(rsis[m], adxs[m], pcode[m], None)
        # Direction-adjusted: a SELL's return is the negative of the move.
        signed = np.where(act == 2, -fw, fw)
        trade = signed[act != 0]
        horizon_stats[h] = (trade, fw)

    # Pattern-gate tolerance sweep - measures how selective the gate is at
    # each tolerance and what the resulting signals actually return.
    gate_stats = {}
    for tol in TOLERANCES:
        codes_t = precompute_pattern_bias_codes(prepared, tolerance=tol)[lo:hi]
        act_t = vectorized_evaluate(rsi, adx, codes_t, None)
        mask = act_t != 0
        signed = np.where(act_t == 2, -fwd, fwd)[mask]
        gate_stats[tol] = (
            float(signed.sum()), float((signed ** 2).sum()), int(mask.sum()),
            float((codes_t != 0).sum()), int(len(codes_t)),
        )

    return {
        "symbol": symbol,
        "fwd": fwd, "action": action, "rsi": rsi, "adx": adx, "codes": codes,
        "single_bull": single_bull, "single_bear": single_bear,
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
    atr = compute_atr(raw).values

    pcode = precompute_pattern_bias_codes(prepared)
    lo, hi = LOOKBACK_MIN, n - FORWARD_WINDOW
    action = vectorized_evaluate(prepared["rsis"][lo:hi], prepared["adxs"][lo:hi], pcode[lo:hi], None)

    variants = (("fixed6", 0.0, False), ("stop_target", 1.5, True),
                ("stop_only", 1.5, False), ("triple_stop", 3.0, True))
    returns = {name: [] for name, _, _ in variants}
    outcomes = {"stop": 0, "target": 0, "timeout": 0}

    for local_i in np.nonzero(action != 0)[0]:
        i = lo + int(local_i)
        entry, a = opens[i + 1], atr[i]
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
    rsi = np.concatenate([p["rsi"] for p in parts])
    adx = np.concatenate([p["adx"] for p in parts])
    codes = np.concatenate([p["codes"] for p in parts])
    single_bull = np.concatenate([p["single_bull"] for p in parts])
    single_bear = np.concatenate([p["single_bear"] for p in parts])

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
    gates = [
        ("ADX >= 20 only", (adx >= 20)),
        ("bullish pattern only", (codes == 1)),
        ("bearish pattern only", (codes == 2)),
        ("RSI < 45 only", (rsi < 45)),
        ("RSI > 55 only", (rsi > 55)),
        ("pattern bull & RSI < 45", (codes == 1) & (rsi < 45)),
        ("pattern bear & RSI > 55", (codes == 2) & (rsi > 55)),
        ("FULL BUY signal", buy),
        ("FULL SELL signal (short view)", sell),
    ]
    for label, mask in gates:
        if label.startswith("FULL SELL"):
            print(_line(label, *_mean_t(-fwd[mask]), int(mask.sum()), baseline_short))
        else:
            print(_line(label, *_mean_t(fwd[mask]), int(mask.sum()), baseline_long))

    print(f"\n{'=' * 78}")
    print("4. PATTERN SOURCE - single-candle pattern vs swing double-top/bottom")
    print(f"{'=' * 78}")
    bull_single = buy & single_bull
    bull_swing = buy & ~single_bull
    bear_single = sell & single_bear
    bear_swing = sell & ~single_bear
    print(_line("BUY driven by hammer/engulfing", *_mean_t(fwd[bull_single]), int(bull_single.sum()), baseline_long))
    print(_line("BUY driven by swing double_bottom", *_mean_t(fwd[bull_swing]), int(bull_swing.sum()), baseline_long))
    print(_line("SELL driven by engulfing", *_mean_t(-fwd[bear_single]), int(bear_single.sum()), baseline_short))
    print(_line("SELL driven by swing double_top", *_mean_t(-fwd[bear_swing]), int(bear_swing.sum()), baseline_short))

    print(f"\n{'=' * 78}")
    print("5. PATTERN GATE FREQUENCY - how often is the gate even ON?")
    print(f"{'=' * 78}")
    print(f"  bullish pattern code on : {(codes == 1).mean() * 100:5.1f}% of candles")
    print(f"  bearish pattern code on : {(codes == 2).mean() * 100:5.1f}% of candles")
    print(f"  any pattern code on     : {(codes != 0).mean() * 100:5.1f}% of candles")
    print(f"  ADX >= 20               : {(adx >= 20).mean() * 100:5.1f}% of candles")
    print(f"  RSI < 45                : {(rsi < 45).mean() * 100:5.1f}% of candles")

    print(f"\n{'=' * 78}")
    print("6. HORIZON SENSITIVITY - is 6 candles simply the wrong hold?")
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

    print(f"\n{'=' * 78}")
    print("7. PATTERN-GATE TOLERANCE SWEEP - making the gate actually selective")
    print(f"{'=' * 78}")
    print("  The swing double-top/bottom match uses a 1.5% price tolerance. If the")
    print("  gate is ON for nearly every candle it is not filtering anything - it is")
    print("  a constant. Tightening it is the most concrete threshold change here.")
    print(f"\n  {'tolerance':>10}  {'gate ON':>9}  {'signals':>9}  {'gross EV':>11}  {'t':>6}")
    for tol in TOLERANCES:
        s = s2 = 0.0
        n_tr = on_cnt = tot = 0.0
        for p in parts:
            gs = p["gate_stats"].get(tol)
            if not gs:
                continue
            s += gs[0]; s2 += gs[1]; n_tr += gs[2]; on_cnt += gs[3]; tot += gs[4]
        if n_tr < 2 or not tot:
            continue
        mean = s / n_tr
        var = max(s2 / n_tr - mean ** 2, 0.0)
        se = np.sqrt(var / n_tr)
        t_stat = mean / se if se > 0 else float("nan")
        print(f"  {tol * 100:>9.1f}%  {on_cnt / tot * 100:>8.1f}%  {int(n_tr):>9}  "
              f"{mean * 100:>+10.4f}%  {t_stat:>+6.1f}")
    print(f"\n  For reference: slippage is {SLIPPAGE * 100:.2f}% per trade, so a gross")
    print("  EV below that loses money regardless of what the gate does.")

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
