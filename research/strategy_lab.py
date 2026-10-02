"""
Strategy lab - measures candidate entries from strategy/candidates.py against
baselines, with realistic exits and costs, on your stored candles.

WHY THIS EXISTS: "add more strategies so profit is more likely" is only
answerable with measurement. research/diagnose_edge.py found the shipped
signal has NO edge and identified which components were positive. This lab
is where replacements get verified BEFORE touching the live agent. A
candidate is not "a new strategy"; it is a hypothesis with a number attached.

WHAT IT MEASURES, per candidate:
  - entries fired by the candidate's exact live code path
    (strategy/candidates.py functions, called per candle)
  - exited at 1.5xATR stop, NO take-profit (the best-measured exit), and
    separately with the 2:1 target for comparison
  - NET of research.backtest.SLIPPAGE
  - against TWO controls: entering at EVERY candle (random-entry baseline)
    and the shipped rule-based signal (BUY and SELL legs separately)

MEASUREMENT DISCIPLINE (the parts that are easy to get wrong):
  1. Non-overlapping trades. RSI<45 persists for many consecutive candles;
     counting every one re-measures the same move dozens of times. After a
     trade exits, the next entry is allowed only one bar later.
  2. Stop wins intrabar ties (we cannot know the intrabar path - same
     conservative assumption as diagnose_edge.py).
  3. Time-split: EV over the first 80% of each symbol's history vs the last
     20%. A candidate that only works in the first 80% is curve-fit noise,
     not a strategy. (Poor man's walk-forward - the real one needs
     walk_forward_optimizer.py extended to exits, which it cannot do yet.)

IN-SAMPLE CAVEAT: everything here is measured on the same history the
candidates were designed from. The time-split is a sanity check, not proof.
Promotion criteria are in docs/STRATEGIES.md.

Usage:
  python -m research.strategy_lab                 # 60 symbols
  python -m research.strategy_lab --n 150
  python -m research.strategy_lab --all
  python -m research.strategy_lab RELIANCE TCS    # specific symbols
  python -m research.strategy_lab --n 40 --max-bars 12 --stop-mult 3.0
"""
import sys
import time as _time

import numpy as np

from core.quant_indicators import load_candles  # noqa: F401  (parity with diagnose_edge imports)
from core.signals import (
    prepare_symbol_series, precompute_pattern_bias_codes, vectorized_evaluate,
    LOOKBACK_MIN, SLIPPAGE,
)
from storage.db import get_watchlist_symbols
from strategy.candidates import CANDIDATES, _swing_arrays

MAX_BARS = 24          # give up waiting for the stop after this many bars
STOP_MULT = 1.5        # x ATR at the signal candle
TARGET_MULT = 2.0      # x stop distance (the live 2:1), reported for comparison
TIME_SPLIT = 0.8       # first 80% of history vs last 20%

def _mean_t(arr: np.ndarray) -> tuple:
    if len(arr) == 0:
        return float("nan"), float("nan")
    m = float(arr.mean())
    sd = float(arr.std(ddof=1))
    t = m / (sd / np.sqrt(len(arr))) if sd > 0 and len(arr) > 1 else float("nan")
    return m, t


def _walk(prepared: dict, entry: float, sign: float, stop_dist: float,
          target_dist: float, use_target: bool, i: int, max_bars: int) -> tuple:
    """One bar-by-bar walk from entry at candle i+1's open. Returns
    (exit_bar, outcome, gross_return). Stop wins intrabar ties - we cannot
    know the intrabar path, so assume the worst."""
    n = prepared["length"]
    highs, lows, closes = prepared["highs"], prepared["lows"], prepared["closes"]
    stop = entry - sign * stop_dist
    target = entry + sign * target_dist
    for b in range(i + 1, min(i + 1 + max_bars, n)):
        if sign > 0:
            hit_stop = lows[b] <= stop
            hit_target = use_target and highs[b] >= target
        else:
            hit_stop = highs[b] >= stop
            hit_target = use_target and lows[b] <= target
        if hit_stop:
            return b, "stop", -stop_dist / entry
        if hit_target:
            return b, "target", target_dist / entry
    j = min(i + max_bars, n - 1)
    return j, "timeout", sign * (closes[j] - entry) / entry


def walk_exit(prepared: dict, atr_arr: np.ndarray, i: int, sign: float,
              stop_mult: float = STOP_MULT, target_mult: float = TARGET_MULT,
              max_bars: int = MAX_BARS) -> tuple | None:
    """Evaluates ONE trade entered at candle i (fill = next candle's open,
    the project-wide convention). Returns (exit_bar, outcome,
    ret_stop_only, ret_stop_target), both NET of SLIPPAGE, or None if the
    entry/ATR is unusable.

    Two INDEPENDENT walks from the same entry: one with the 2:1 take-profit
    (the live exit) and one stop-only (the best-measured exit per
    diagnose_edge.py). Keeping them independent is what makes the
    stop-only numbers honest - after a target hit, a stop-only trade keeps
    running and may still stop out later."""
    entry = prepared["opens"][i + 1]
    atr = atr_arr[i]
    if not np.isfinite(entry) or entry <= 0 or not np.isfinite(atr) or atr <= 0:
        return None

    stop_dist = stop_mult * atr
    target_dist = target_mult * stop_dist
    b_st, outcome_st, ret_st = _walk(prepared, entry, sign, stop_dist, target_dist, True, i, max_bars)
    _, _, ret_so = _walk(prepared, entry, sign, stop_dist, target_dist, False, i, max_bars)
    return b_st, outcome_st, ret_so - SLIPPAGE, ret_st - SLIPPAGE


def _collect(symbol: str, max_bars: int, stop_mult: float = STOP_MULT) -> dict | None:
    """Evaluates every candidate + controls on one symbol."""
    prepared = prepare_symbol_series(symbol)
    if prepared is None or prepared["length"] < 500:
        return None

    n = prepared["length"]
    atr_arr = prepared["atrs"]
    # Candidates read the NORMALIZED swing arrays; backtest's
    # precompute_pattern_bias_codes() needs the ORIGINAL list-of-tuples form.
    # Keep both - mutating the original in place broke the pattern precompute
    # (first lab run crashed here; kept as a comment so nobody "simplifies"
    # it back).
    prepared["swings_norm"] = _swing_arrays(prepared["swings"])
    split_i = int(n * TIME_SPLIT)

    results = {name: {"ret_so": [], "ret_st": [], "hold": [], "early": [], "late": [],
                      "out": {"stop": 0, "target": 0, "timeout": 0}}
               for name in list(CANDIDATES) + ["_baseline", "_shipped_buy", "_shipped_all"]}

    def record(bucket: dict, i: int, sign: float):
        w = walk_exit(prepared, atr_arr, i, sign, stop_mult=stop_mult, max_bars=max_bars)
        if w is None:
            return None
        exit_bar, outcome, ret_so, ret_st = w
        bucket["ret_so"].append(ret_so)
        bucket["ret_st"].append(ret_st)
        bucket["hold"].append(exit_bar - i)
        (bucket["early"] if i < split_i else bucket["late"]).append(ret_so)
        if outcome == "stop":
            bucket["out"]["stop"] += 1
        elif outcome == "target":
            bucket["out"]["target"] += 1
        else:
            bucket["out"]["timeout"] += 1
        return exit_bar

    # --- candidates + random-entry baseline: non-overlapping longs ---------
    strategies = {name: fn for name, fn in CANDIDATES.items()}
    next_free = {name: LOOKBACK_MIN for name in strategies}
    base_next_free = LOOKBACK_MIN
    for i in range(LOOKBACK_MIN, n - 2):
        for name, fn in strategies.items():
            if i < next_free[name]:
                continue
            if fn(prepared, i):
                exit_bar = record(results[name], i, 1.0)
                next_free[name] = (exit_bar if exit_bar is not None else i) + 1
        if i >= base_next_free:
            w_ok = np.isfinite(atr_arr[i]) and atr_arr[i] > 0 and np.isfinite(prepared["opens"][i + 1]) \
                and prepared["opens"][i + 1] > 0
            if w_ok:
                exit_bar = record(results["_baseline"], i, 1.0)
                base_next_free = (exit_bar if exit_bar is not None else i) + 1

    # --- shipped signal reference (same exits, non-overlapping) ------------
    pcode = precompute_pattern_bias_codes(prepared)
    lo, hi = LOOKBACK_MIN, n - 2
    actions = vectorized_evaluate(
        prepared["ema_fast"][lo:hi],
        prepared["ema_slow"][lo:hi],
        prepared["ema_pullback"][lo:hi],
        prepared["htf_trend_up"][lo:hi],
        prepared["htf_trend_down"][lo:hi],
        prepared["adxs"][lo:hi],
        prepared["atrs"][lo:hi],
        prepared["closes"][lo:hi],
        prepared["rsis"][lo:hi],
        prepared["volume_ratio"][lo:hi],
        None
    )
    shipped_next = LOOKBACK_MIN
    for local_i in np.nonzero(actions != 0)[0]:
        i = lo + int(local_i)
        if i < shipped_next:
            continue
        sign = 1.0 if actions[local_i] == 1 else -1.0
        exit_bar = record(results["_shipped_all"], i, sign)
        if actions[local_i] == 1:
            # BUY leg gets its own bucket (short leg measured separately above).
            # Its outcome tally is counted here because record() above may have
            # recorded a SELL (the shares-only column would otherwise show 0%).
            w = walk_exit(prepared, atr_arr, i, 1.0, stop_mult=stop_mult, max_bars=max_bars)
            if w is not None:
                results["_shipped_buy"]["ret_so"].append(w[2])
                results["_shipped_buy"]["ret_st"].append(w[3])
                results["_shipped_buy"]["hold"].append(w[0] - i)
                (results["_shipped_buy"]["early"] if i < split_i else results["_shipped_buy"]["late"]).append(w[2])
                results["_shipped_buy"]["out"][w[1] if w[1] in ("stop", "target") else "timeout"] += 1
        shipped_next = (exit_bar if exit_bar is not None else i) + 1

    return results


def main(symbols: list, max_bars: int = MAX_BARS, stop_mult: float = STOP_MULT) -> None:
    t0 = _time.time()
    pooled = {name: {"ret_so": [], "ret_st": [], "hold": [], "early": [], "late": [],
                     "out": {"stop": 0, "target": 0, "timeout": 0}}
              for name in list(CANDIDATES) + ["_baseline", "_shipped_buy", "_shipped_all"]}
    used = 0
    for k, symbol in enumerate(symbols, 1):
        r = _collect(symbol, max_bars, stop_mult=stop_mult)
        if r is None:
            continue
        used += 1
        for name, bucket in r.items():
            for key in ("ret_so", "ret_st", "hold", "early", "late"):
                pooled[name][key].extend(bucket[key])
            for o, c in bucket["out"].items():
                pooled[name]["out"][o] += c
        if k % 10 == 0:
            print(f"  ...{k}/{len(symbols)} symbols ({_time.time() - t0:.0f}s)")

    if not used:
        print("No usable symbols - run `python -m ingest.fetch_historical` first.")
        return

    print(f"\n{'=' * 100}")
    print(f"STRATEGY LAB - {used} symbols, exit = {stop_mult}xATR stop, max {max_bars} bars, "
          f"net of {SLIPPAGE * 100:.2f}% slippage, NON-OVERLAPPING trades")
    print(f"{'=' * 100}")
    hdr = (f"  {'strategy':<24} {'trades':>7} {'win%':>6} {'EV/trade':>10} {'t':>6} "
           f"{'hold':>5} {'stop%':>6} {'EV w/target':>11} {'EV early80':>10} {'EV late20':>10}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    labels = {
        "_baseline": "baseline (every candle)",
        "_shipped_buy": "shipped signal, BUY only",
        "_shipped_all": "shipped signal, BUY+SELL",
    }
    order = ["_baseline", "_shipped_buy", "_shipped_all"] + list(CANDIDATES)
    for name in order:
        b = pooled[name]
        so = np.asarray(b["ret_so"])
        st = np.asarray(b["ret_st"])
        if len(so) == 0:
            print(f"  {labels.get(name, name):<24} {'0':>7}")
            continue
        m, t = _mean_t(so)
        m_st, _ = _mean_t(st)
        m_e, _ = _mean_t(np.asarray(b["early"]))
        m_l, _ = _mean_t(np.asarray(b["late"]))
        win = float((so > 0).mean()) * 100
        hold = float(np.mean(b["hold"]))
        out = b["out"]
        total_out = max(sum(out.values()), 1)
        label = labels.get(name, name)
        print(f"  {label:<24} {len(so):>7} {win:>6.1f} {m * 100:>+9.4f}% {t:>+6.1f} "
              f"{hold:>5.1f} {out['stop'] / total_out * 100:>5.1f}% {m_st * 100:>+10.4f}% "
              f"{m_e * 100:>+9.4f}% {m_l * 100:>+9.4f}%")

    print(f"""
  READING THIS
  -----------
  EV/trade is NET of slippage - positive is the only thing that matters.
  t >= 2 is the minimum to take a number seriously at all.
  'EV early80' vs 'EV late20' is the honesty check: a real edge survives
  into the last 20%; one that only worked early is curve-fit.
  'stop%' near 100 with negative EV means the stop is too tight for this
  entry's noise - try wider stops before discarding the entry.
  Promotion rules live in docs/STRATEGIES.md. Nothing here auto-trades.
  ({_time.time() - t0:.0f}s total)""")


if __name__ == "__main__":
    args = sys.argv[1:]
    max_bars, stop_mult = MAX_BARS, STOP_MULT
    if "--max-bars" in args:
        k = args.index("--max-bars")
        max_bars = int(args[k + 1])
        args = args[:k] + args[k + 2:]
    if "--stop-mult" in args:
        k = args.index("--stop-mult")
        stop_mult = float(args[k + 1])
        args = args[:k] + args[k + 2:]
    if not args:
        symbols = get_watchlist_symbols()[:60]
    elif args[0] == "--all":
        symbols = get_watchlist_symbols()
    elif args[0] == "--n":
        symbols = get_watchlist_symbols()[: int(args[1])]
    else:
        symbols = args
    main(symbols, max_bars=max_bars, stop_mult=stop_mult)
