"""
Session, regime and cost features for candidate entry strategies.

WHY THIS EXISTS: research/diagnose_edge.py ablated the shipped rule signal
and found no component with an edge larger than trading costs, and
docs/STRATEGIES.md records the conclusion that follows from that:

    "Frequency selection barely works intraday. ... If a real edge exists
     here, it is more likely in *when not to trade* (time-of-day, event
     filters, spread-aware cost models) than in another pattern gate."

So this module supplies the inputs that answer "when NOT to trade",
which the current stack (RSI / ADX / candlestick patterns) has no way to
express at all:

  - WHERE IN THE SESSION we are. The first 30 minutes of a cash session
    are dominated by the opening auction and gap resolution, where a
    5-minute stop is mostly noise; the last hour drifts into the auction
    close. Neither is where a 6-bar momentum read is informative.
  - HOW VOLATILE this name is RIGHT NOW, relative to its own recent
    history (ATR percentile). A 1.5xATR stop means something very
    different in the calm 20th percentile than in the 95th, and the
    lab's own "stop% near 100" note is the symptom of ignoring this.
  - WHERE THE day's volume actually transacted (session VWAP). Price
    above a rising day VWAP is the standard "is this stock being
    accumulated" reference; the existing stack has no reference price
    other than its own moving averages.
  - Whether the HIGHER timeframes agree. The stack is single-timeframe
    (5-minute), so it cannot tell a pullback in an uptrend from a
    pullback in a downtrend that merely looks similar on 5 minutes.
  - Whether one bar's typical move can even cover the round trip. This
    is the cost-aware gate: if a name's ATR is a small fraction of a
    percent, a 6-bar hold cannot pay 0.05% round trip plus a real edge.

EVERYTHING HERE IS CAUSAL. Every array at index i is built only from
candles at indices <= i. There is no centering, no full-series
normalization, no interpolation - the same rule as
research.backtest.prepare_symbol_series(). A rolling rank is a function
of the trailing window, a VWAP is a cumulative sum within the day so
far, and a swing-style offset is never applied. That matters because
these features are read both by research/strategy_lab.py (over history)
and strategy/shadow.py (live): a feature that quietly peeked at the
future would show a fantastic lab number and fail the moment it went
live.

PURITY AND COST: build() is called once per symbol and memoized on the
`prepared` dict, because the lab calls each candidate function per candle
per symbol (a 19,000-candle symbol x 11 candidates = ~209,000 calls, and
re-deriving a VWAP and a rolling percentile rank in each of those would
dominate the runtime). The cache key lives on the dict that is already
scoped to a single symbol, so this is memoization of a pure function,
not hidden state - the same `prepared` always yields the same features.

These are FEATURES, not gates. Which combination of them to trade is a
hypothesis that has to earn it in research/strategy_lab.py against the
random-entry baseline, and the gates that read them live in
strategy/candidates.py alongside every other candidate. Nothing in this
file changes what the live agent trades.
"""
import numpy as np
import pandas as pd

from core.freshness import IST, MARKET_CLOSE, MARKET_OPEN
from research.backtest import FORWARD_WINDOW

# Key the memoized feature bundle is stored under on the `prepared` dict.
CACHE_KEY = "_session_features"

# The project stores 5-minute candles. Used to convert "minutes until the
# close" into "bars remaining", and to keep that conversion honest if the
# candle interval ever changes.
CANDLE_MINUTES = 5

# Candles outside the NSE cash session that are still legitimate rows in the
# stored data (Kite reports post-market prints). Used only by the sanity
# check, which must not flag real data as broken - see verify_sanity().
POST_MARKET_END_MINUTE = 16 * 60 + 30

DEFAULTS = {
    # Bars used to judge whether session VWAP is rising. 12 x 5min = 1 hour,
    # long enough that a single bar's noise cannot flip the sign.
    "vwap_slope_bars": 12,
    # Window for the ATR percentile. 250 x 5min = ~3.3 trading days, so the
    # "current volatility" read adapts across a week without chasing a
    # single quiet afternoon.
    "atr_pctile_window": 250,
    "atr_pctile_min_periods": 60,
    # Volume z-score window - 20 bars is one session, which is the natural
    # baseline for "is anyone trading this right now".
    "volume_z_window": 20,
    # Higher-timeframe proxies built by resampling the 5-minute series in
    # index space: 48 bars = 4 hours, 240 bars = one full trading day.
    # The project stores 5-minute candles only, so these are the cheapest
    # honest multi-timeframe read available without new data.
    "htf_fast_bars": 48,
    "htf_slow_bars": 240,
}


def _clock_parts(timestamps) -> tuple:
    """(minute_of_day, day_index) arrays from prepared["timestamps"].

    The candles table stores timestamps in two forms - Kite's tz-aware
    ISO strings ('2025-09-29T09:15:00+05:30') and naive local ones
    ('2025-09-29T09:15:00') - for the same minute, which core/freshness.py
    documents. Slicing the wall-clock characters off the string form works
    for BOTH, because the +05:30 suffix means the wall-clock part is
    already IST. A datetime-object column is normalized to IST first.

    minute_of_day is -1 where unparseable; day_index is 0 where
    unparseable, so downstream grouping stays contiguous and a bad
    timestamp degrades to "unknown session position" (every session gate
    then declines to fire) rather than to a plausible-looking wrong value.
    """
    s = pd.Series(timestamps)
    all_strings = s.map(lambda v: isinstance(v, str)).all()
    if all_strings:
        minutes = s.str.slice(11, 13).astype("float64") * 60 + s.str.slice(14, 16).astype("float64")
        days = s.str.slice(0, 10)
    else:
        dt = pd.to_datetime(s, format="mixed", utc=True).dt.tz_convert(IST)
        minutes = dt.dt.hour.astype("float64") * 60 + dt.dt.minute.astype("float64")
        days = dt.dt.strftime("%Y-%m-%d")

    minute = minutes.fillna(-1.0).to_numpy(dtype=float)
    day_index = pd.factorize(days.fillna(""))[0].astype(np.int32)
    return minute, day_index


def _safe_ratio(num: np.ndarray, den: np.ndarray) -> np.ndarray:
    """num/den with non-finite and zero-denominator results as NaN.

    NaN rather than 0 or inf: every gate downstream treats NaN as "cannot
    evaluate" and declines, which is the safe direction. A silent 0.0
    would read as "exactly at VWAP" and let the trade through.
    """
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.asarray(num, dtype=float) / np.asarray(den, dtype=float)
    out[~np.isfinite(out)] = np.nan
    return out


def _group_cumsum(values: np.ndarray, groups: np.ndarray) -> np.ndarray:
    """Cumulative sum of `values` restarted at every change in `groups`.

    This is what makes VWAP a SESSION vwap rather than an all-time
    average: it only ever accumulates bars from the current day, and
    never from a previous session.
    """
    frame = pd.DataFrame({"g": groups, "v": values})
    return frame.groupby("g", sort=False)["v"].cumsum().to_numpy(dtype=float)


def build(prepared: dict, params: dict = None) -> dict:
    """Builds every session/regime feature array for one symbol.

    Returns a dict of same-length float arrays, all aligned to
    prepared["length"]. See the module docstring for the causality rule
    each one obeys.
    """
    p = {**DEFAULTS, **(params or {})}
    n = prepared["length"]

    closes = np.asarray(prepared["closes"], dtype=float)
    opens = np.asarray(prepared["opens"], dtype=float)
    highs = np.asarray(prepared["highs"], dtype=float)
    lows = np.asarray(prepared["lows"], dtype=float)
    volumes = np.asarray(prepared["volumes"], dtype=float)
    atrs = np.asarray(prepared["atrs"], dtype=float)

    minute, day = _clock_parts(prepared["timestamps"])

    # --- position within the trading day ---------------------------------
    frame = pd.DataFrame({"day": day})
    bars_into_day = frame.groupby("day").cumcount().to_numpy(dtype=float)

    # Bars remaining in the session, derived from the CLOCK, never from the
    # day group's total size. This is not a micro-optimisation: group size is
    # computed over the whole series, so "bars left today" would know how many
    # candles the rest of the session contains - a lookahead that verify_causal()
    # below catches by comparing a full run against a truncated prefix. The
    # session's end is a known constant (core/freshness.MARKET_CLOSE), so the
    # honest causal version is the number of 5-minute slots left before it.
    close_minute = MARKET_CLOSE.hour * 60 + MARKET_CLOSE.minute
    bars_left = np.full(n, np.nan)
    known = minute >= 0
    bars_left[known] = np.maximum(
        0.0, np.ceil((close_minute - minute[known]) / float(CANDLE_MINUTES))
    )

    # --- session VWAP (day-anchored) and distance from it -----------------
    typical = (highs + lows + closes) / 3.0
    pv_cum = _group_cumsum(typical * volumes, day)
    v_cum = _group_cumsum(volumes, day)
    vwap = _safe_ratio(pv_cum, v_cum)
    dist_vwap = _safe_ratio(closes - vwap, vwap)
    # Explicitly shifted, never np.roll: rolling wraps around, which would
    # make the first `slope_bars` bars of the series compare against the END
    # of the series - a lookahead, and a nonsense one.
    slope_bars = int(p["vwap_slope_bars"])
    vwap_slope = np.full(n, np.nan)
    if n > slope_bars:
        past = vwap[:-slope_bars]
        vwap_slope[slope_bars:] = _safe_ratio(vwap[slope_bars:] - past, past)

    # --- volatility regime: ATR as % of price, and its trailing percentile -
    atr_pct = _safe_ratio(atrs, closes)
    atr_pctile = (
        pd.Series(atr_pct)
        .rolling(p["atr_pctile_window"], min_periods=p["atr_pctile_min_periods"])
        .rank(pct=True)
        .to_numpy(dtype=float)
    )

    # --- participation: is anyone trading this right now? -----------------
    v_ma = pd.Series(volumes).rolling(p["volume_z_window"], min_periods=p["volume_z_window"]).mean()
    v_sd = pd.Series(volumes).rolling(p["volume_z_window"], min_periods=p["volume_z_window"]).std(ddof=0)
    volume_z = _safe_ratio(volumes - v_ma.to_numpy(), v_sd.to_numpy())

    # --- higher-timeframe agreement (index-space resample of 5-min bars) --
    fast, slow = p["htf_fast_bars"], p["htf_slow_bars"]
    htf_fast_ret = np.full(n, np.nan)
    htf_slow_ret = np.full(n, np.nan)
    if n > fast:
        htf_fast_ret[fast:] = closes[fast:] / closes[:-fast] - 1.0
    if n > slow:
        htf_slow_ret[slow:] = closes[slow:] / closes[:-slow] - 1.0

    # --- opening gap, only on the first bar of a day ----------------------
    prev_close = np.roll(closes, 1)
    prev_close[:1] = np.nan
    gap = _safe_ratio(opens - prev_close, prev_close)
    gap[bars_into_day != 0] = np.nan  # an intraday move is not a gap

    # --- cost cover: can one bar's typical move pay for the round trip? ---
    # atr_pct IS the per-bar expected move as a fraction of price, so
    # comparing it directly against the round-trip cost is dimensionally
    # correct. The sqrt() forward term scales it to the hold horizon the
    # lab actually measures at (FORWARD_WINDOW bars).
    expected_move = atr_pct * np.sqrt(FORWARD_WINDOW)

    return {
        "minute_of_day": minute,
        "day_index": day,
        "bars_into_day": bars_into_day,
        "bars_left_in_day": bars_left,
        "vwap": vwap,
        "dist_vwap": dist_vwap,
        "vwap_slope": vwap_slope,
        "atr_pct": atr_pct,
        "atr_pctile": atr_pctile,
        "volume_z": volume_z,
        "htf_fast_ret": htf_fast_ret,
        "htf_slow_ret": htf_slow_ret,
        "gap_pct": gap,
        "expected_move": expected_move,
    }


def features(prepared: dict, params: dict = None) -> dict:
    """build() with memoization on the `prepared` dict.

    Safe to call from a per-candidate, per-candle loop: the bundle is
    derived once per symbol and reused, and because the key is stored on
    the symbol-scoped dict it can never leak between symbols.
    """
    cached = prepared.get(CACHE_KEY)
    if cached is not None:
        return cached
    bundle = build(prepared, params)
    prepared[CACHE_KEY] = bundle
    return bundle


# ---------------------------------------------------------------------------
# Causality self-test
# ---------------------------------------------------------------------------

def _prefix(prepared: dict, k: int) -> dict:
    """The same symbol truncated to its first k candles.

    Every array the features are derived from is sliced to length k; the
    feature dict itself carries no length, so nothing else needs changing.
    """
    short = {}
    for key, value in prepared.items():
        if key == CACHE_KEY or not isinstance(value, np.ndarray):
            short[key] = value
        else:
            short[key] = value[:k]
    short["length"] = k
    return short


def verify_causal(symbol: str, candles: dict, params: dict = None) -> tuple:
    """Proves these features cannot see the future, on real data.

    THE TEST: build the features over the symbol's FULL history, then
    build them again over a truncated prefix, and require the prefix's
    values to be IDENTICAL to the full run's first k values.

    This catches exactly the failure that matters here and that eyeballing
    an indicator never does. A lookahead in these features would still
    produce plausible-looking numbers in the lab - it would just be a
    better-looking number than the strategy deserves, and it would
    evaporate the first time it ran live. Any feature that peeked ahead
    (a centered window, a full-series normalization, a bfill) changes its
    value at index i once later candles exist, so the prefix comparison
    fails on it by construction. `research/backtest.py --selftest` uses the
    same idea (fast path vs slow reference) to earn the same confidence.

    Returns (ok, list_of_mismatched_feature_names)."""
    k = int(candles["length"] * 0.7)
    if k < 400:
        return True, []  # not enough history to make the comparison meaningful

    full = build(candles, params)
    truncated = build(_prefix(candles, k), params)

    mismatched = []
    for name, values in truncated.items():
        reference = full[name][:k]
        both_nan = np.isnan(values) & np.isnan(reference)
        close = np.isclose(values, reference, rtol=1e-9, atol=1e-12, equal_nan=False)
        if not np.all(close | both_nan):
            mismatched.append(name)
    return (not mismatched), mismatched


def verify_sanity(symbol: str, candles: dict, params: dict = None) -> list:
    """Cheap structural checks that catch unit/scale mistakes.

    A feature that is causally correct but off by a factor, or anchored to
    the wrong session, passes the causality test perfectly - so it needs its
    own assertions. Returns a list of human-readable problems (empty = OK).
    """
    problems = []
    bundle = build(candles, params)
    n = candles["length"]
    closes = np.asarray(candles["closes"], dtype=float)
    lows = np.asarray(candles["lows"], dtype=float)
    highs = np.asarray(candles["highs"], dtype=float)

    minutes = bundle["minute_of_day"]
    session = minutes[np.isfinite(minutes) & (minutes >= 0)]
    open_minute = MARKET_OPEN.hour * 60 + MARKET_OPEN.minute
    close_minute = MARKET_CLOSE.hour * 60 + MARKET_CLOSE.minute
    if len(session) == 0:
        problems.append("no parseable session timestamps")
    else:
        if session.min() < open_minute or session.max() > POST_MARKET_END_MINUTE:
            problems.append(
                f"minute_of_day outside any plausible trading time: "
                f"{int(session.min())//60:02d}:{int(session.min())%60:02d}"
                f"-{int(session.max())//60:02d}:{int(session.max())%60:02d}")
        # Not a failure: the stored data legitimately contains a few
        # post-market prints, and gate_session_window() declines them. Worth
        # counting so a sudden jump in this number is visible.
        after_hours = int((session > close_minute).sum())
        if after_hours:
            print(f"    note: {after_hours} post-market candle(s) present - "
                  f"the session window gate excludes them")

    # VWAP must be a price the day actually traded at.
    vwap = bundle["vwap"]
    ok = np.isfinite(vwap)
    if np.any(vwap[ok] < lows[ok].min() * 0.99) or np.any(vwap[ok] > highs[ok].max() * 1.01):
        problems.append("session VWAP falls outside the observed price range")

    pctile = bundle["atr_pctile"]
    finite_pctile = pctile[np.isfinite(pctile)]
    if len(finite_pctile) and (finite_pctile.min() < 0 or finite_pctile.max() > 1):
        problems.append("atr_pctile outside [0, 1] - it is a percentile, not a price")

    bars_into = bundle["bars_into_day"]
    if np.nanmax(bars_into) > 100:
        problems.append(f"bars_into_day max {int(np.nanmax(bars_into))} - day grouping is broken")

    bars_left = bundle["bars_left_in_day"]
    finite_left = bars_left[np.isfinite(bars_left)]
    if len(finite_left) and finite_left.max() > 100:
        problems.append(f"bars_left_in_day max {int(finite_left.max())} - "
                        f"the session-length conversion is wrong")

    for name in ("atr_pct", "expected_move", "dist_vwap"):
        values = bundle[name]
        if np.any(np.isinf(values)):
            problems.append(f"{name} contains inf (division by zero not neutralized)")

    if n > DEFAULTS["htf_slow_bars"] and not np.any(np.isfinite(bundle["htf_slow_ret"])):
        problems.append("htf_slow_ret is all-NaN despite enough history")

    # The session VWAP must reset: the first bar of a day has no prior
    # volume to carry over, so its VWAP is its own typical price.
    first_bars = np.flatnonzero(bars_into == 0)
    if len(first_bars):
        typical = (highs[first_bars] + lows[first_bars] + closes[first_bars]) / 3.0
        if not np.allclose(bundle["vwap"][first_bars], typical, rtol=1e-6):
            problems.append("session VWAP does not reset at the start of each day")
    return problems


def report_cost_cover(candles_list: list) -> dict:
    """Distribution of the expected move, in units of the round-trip cost.

    This is the evidence behind a claim made in three documents: that
    cost-aware entry gating is inert on this data. The `expected_move`
    feature is ATR-%-of-price scaled to the hold horizon, so comparing it to
    `SLIPPAGE` answers "could the move this trade waits for even pay for
    taking it?" If the answer is yes on ~every candle, the gate has nothing
    to filter — and the honest conclusion is that cost is not the binding
    constraint here, direction is.

    Zero-ATR bars are excluded: they are unfinished or zero-range candles
    (a data artefact), not a tradable calm state, and including them makes
    the minimum meaningless.
    """
    from research.backtest import SLIPPAGE

    collected = []
    for candles in candles_list:
        expected = build(candles)["expected_move"]
        collected.append(expected[np.isfinite(expected) & (expected > 0)])
    if not collected:
        return {}

    values = np.concatenate(collected)
    percentiles = np.percentile(values, [1, 5, 25, 50, 75, 95, 99])
    return {
        "candles": int(values.size),
        "multiples": {f"p{p}": float(values_p / SLIPPAGE)
                      for p, values_p in zip((1, 5, 25, 50, 75, 95, 99), percentiles)},
        "share_below_1x": float((values < SLIPPAGE).mean()),
        "share_below_3x": float((values < 3 * SLIPPAGE).mean()),
    }


if __name__ == "__main__":
    import sys

    from research.backtest import prepare_symbol_series
    from storage.db import get_watchlist_symbols

    args = sys.argv[1:]
    if args and args[0] == "--cost-cover":
        # The distribution behind the "cost gating is inert" claim in
        # README.md, docs/STRATEGIES.md and docs/EDGE_ANALYSIS.md.
        limit = int(args[1]) if len(args) > 1 and args[1].isdigit() else 20
        prepared = []
        for sym in get_watchlist_symbols()[:limit]:
            series = prepare_symbol_series(sym)
            if series is not None and series["length"] >= 2000:
                prepared.append(series)
        report = report_cost_cover(prepared)
        if not report:
            print("No usable symbols - run `python -m ingest.fetch_historical` first.")
            sys.exit(1)
        print(f"Expected move vs the round trip, {report['candles']} candles "
              f"across {len(prepared)} symbols:")
        for name, multiple in report["multiples"].items():
            print(f"  {name:>4}: {multiple:6.1f}x the round trip")
        print(f"  candles below 1x cost: {report['share_below_1x'] * 100:.3f}%")
        print(f"  candles below 3x cost: {report['share_below_3x'] * 100:.3f}%")
        print("\nIf nearly every candle clears the cost line by an order of magnitude,\n"
              "no cost-aware entry gate can help: the loss is directional, not sized.")
        sys.exit(0)

    symbols = args if args else get_watchlist_symbols()[:5]
    all_ok = True
    for sym in symbols:
        prepared = prepare_symbol_series(sym)
        if prepared is None or prepared["length"] < 1000:
            print(f"  {sym}: skipped (not enough history)")
            continue
        ok, bad = verify_causal(sym, prepared)
        problems = verify_sanity(sym, prepared)
        status = "OK" if (ok and not problems) else "FAILED"
        print(f"  {sym}: {status}"
              + (f" - LOOKAHEAD in {bad}" if bad else "")
              + (f" - {'; '.join(problems)}" if problems else ""))
        all_ok = all_ok and ok and not problems

    print("\nSELFTEST PASSED - no feature depends on a future candle, all structure sane"
          if all_ok else
          "\nSELFTEST FAILED - do not trust these features in the lab until this is fixed")
    sys.exit(0 if all_ok else 1)
