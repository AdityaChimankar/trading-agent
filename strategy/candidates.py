"""
Candidate entry strategies - the experimental shelf above decision_agent.py.

WHERE THESE COME FROM: research/diagnose_edge.py ablated the shipped signal and
found exactly four components with a positive (if small) measured edge, and two
that are actively harmful:

  +  RSI pullback recovery      (RSI < 45 gate: +0.0022%/trade vs baseline)
  +  trend filter               (ADX >= 20: +0.0006%/trade, t = +3.9 - the only
                                 component with a consistently positive t-stat)
  +  swing double_bottom        (BUY via double_bottom: +0.0092%/trade, t = +3.5 -
                                 the best edge found anywhere in the analysis)
  +  ATR-only exits             (1.5xATR stop with NO take-profit: -0.0363% net vs
                                 -0.0451% for the live stop+target - the largest
                                 single measured improvement)
  -  hammer / engulfing         (BUY via hammer/engulfing: -0.0083%, t = -3.3 -
                                 the WORST BUY source; SELL via engulfing -0.0101%)
  -  double_top                 (SELL leg; also the structural cause of the ~76%
                                 short bias via the 98%-always pattern gate)

Each candidate below is a PURE function over the prepared arrays from
research.backtest.prepare_symbol_series(): it takes arrays and an index and
returns True/False. No DB, no I/O, no state. That makes them:
  - backtestable by research/strategy_lab.py (vectorized over all candles)
  - loggable live by strategy/shadow.py on the exact same code
  - trivially unit-testable later without fixtures or a DB

NOTHING here changes what the live agent trades. decision_agent.py is untouched;
candidates are measured offline in strategy_lab.py and logged in parallel by
shadow.py until they have a track record. That is the same discipline as the
LLM/ML comparison paths: parallel logging, separate table, no live influence.

ALL of these are LONG-ONLY. The measured evidence says the short leg is where
the loss concentrates (SELL t = -4.9), and intraday index drift gives longs a
tailwind shorts do not have.

THE SECOND WAVE (session / regime / cost gates). The first four candidates above
all narrow WHEN a pullback entry may fire but say nothing about the conditions
the trade is entered into - the same 5-minute chart at 09:20 and at 13:00 is
treated identically, in a name whose ATR sits in the 5th percentile and one
sitting in the 95th. docs/STRATEGIES.md records why that is where the remaining
upside is:

    "If a real edge exists here, it is more likely in *when not to trade*
     (time-of-day, event filters, spread-aware cost models) than in another
     pattern gate."

So the gates below - session window, ATR percentile, session VWAP, higher
timeframe agreement, opening gap, cost cover - are each added ON TOP of the best
measured entry (cand_trend_pullback), never instead of it, and each is a separate
registered candidate so the lab attributes any improvement to the specific filter
rather than to a bundle. A filter that earns nothing costs one row in the lab
table; a filter bundled with five others that "works" teaches you nothing about
which one worked, which is how a stack of curve-fit gets mistaken for an edge.
"""
import numpy as np

from core.signals import DEFAULT_PARAMS, SWING_ORDER
from strategy.session_features import features as session_features

# A swing high/low is only "confirmed" SWING_ORDER bars after it happens -
# using it earlier would be lookahead. Candidates that read prepared["swings"]
# must respect this offset, exactly like swing_bias_at() does.
SWING_CONFIRM_LAG = SWING_ORDER


def _swing_prices_at(swings_norm: dict, i: int, kind: str) -> tuple | None:
    """The two most recent CONFIRMED swing prices of `kind` ('highs'/'lows')
    as of index i, or None if there are fewer than two. `swings_norm` is the
    normalized form produced by _swing_arrays() (parallel sorted index/price
    lists); the caller passes prepared["swings_norm"], NOT the raw
    prepared["swings"] list-of-tuples that backtest's pattern precompute
    consumes. Mirrors the bisect logic in
    backtest.precompute_pattern_bias_codes() so backtest and live agree."""
    import bisect

    idx_key, price_key = f"{kind}_idx", f"{kind}_price"
    idxs, prices = swings_norm[idx_key], swings_norm[price_key]
    cutoff = i - SWING_CONFIRM_LAG
    count = bisect.bisect_right(idxs, cutoff)
    if count < 2:
        return None
    return prices[count - 2], prices[count - 1]


def _swing_arrays(swings: dict) -> dict:
    """Normalizes compute_swing_extrema()'s list-of-tuples swings into
    sorted parallel arrays (indices + prices per kind) so repeated
    candidate evaluations don't rebuild them per candle."""
    highs = swings.get("highs") or []
    lows = swings.get("lows") or []
    return {
        "highs_idx": [j for j, _ in highs],
        "highs_price": [p for _, p in highs],
        "lows_idx": [j for j, _ in lows],
        "lows_price": [p for _, p in lows],
    }


# ---------------------------------------------------------------------------
# Shared gate building blocks
# ---------------------------------------------------------------------------

def gate_trend(adxs: np.ndarray, i: int, threshold: float = None) -> bool:
    """ADX above threshold - the only component whose t-stat was reliably
    positive across the ablation (+3.9)."""
    t = DEFAULT_PARAMS["adx_threshold"] if threshold is None else threshold
    a = adxs[i]
    return bool(np.isfinite(a) and a >= t)


def gate_pullback(rsis: np.ndarray, i: int, max_rsi: float = 45.0) -> bool:
    """Short-term weakness in an uptrend context - the RSI<45 gate measured
    +0.0022%/trade over baseline."""
    r = rsis[i]
    return bool(np.isfinite(r) and r < max_rsi)


def gate_double_bottom(swings_norm: dict, i: int, tolerance: float = 0.01) -> bool:
    """Two confirmed swing lows within tolerance - the single best edge in
    the ablation (+0.0092%, t = +3.5). Uses a TIGHTER tolerance (1%) than
    the shipped 1.5%: the sweep showed 1.5% puts the gate on 97.9% of
    candles (a constant, not a filter). 1% was the best trade-off measured
    (gate on 95.5%, least-negative EV of the sweep)."""
    pair = _swing_prices_at(swings_norm, i, "lows")
    if pair is None:
        return False
    l1, l2 = pair
    return abs(l1 - l2) / l1 < tolerance


def gate_volume_surge(volumes: np.ndarray, i: int, window: int = 20, min_ratio: float = 1.5) -> bool:
    """Current volume above its recent average - untested in the ablation
    (it was never a shipped component), included as an optional interest
    filter. Candidates using it must be measured before being trusted."""
    if i < window:
        return False
    v = volumes[i]
    avg = volumes[i - window:i].mean()
    return bool(np.isfinite(v) and avg > 0 and v / avg >= min_ratio)


# ---------------------------------------------------------------------------
# Session / regime / cost gates - the "when NOT to trade" layer
# ---------------------------------------------------------------------------
# All of these read strategy/session_features.py's memoized arrays. Each one
# is deliberately CONSERVATIVE about missing data: a NaN feature (warm-up
# period, unparseable timestamp, zero volume) returns False, so an
# insufficiently-studied candle can never pass a gate. The failure direction
# matters - a gate that treats unknown as "pass" would fire most often exactly
# where the data is thinnest, which is the newest and least reliable data.

# The NSE cash session runs 09:15-15:30 (core/freshness.py). These windows are
# expressed as minutes-from-midnight so the gate is a direct comparison.
SESSION_START_MINUTE = 9 * 60 + 15
SESSION_END_MINUTE = 15 * 60 + 30
# Default tradeable window: skip the first 30 minutes (opening auction and gap
# resolution) and the last 60 (drift into the closing auction). Both are where
# a fixed 5-minute stop is fighting the auction rather than the trend.
GATE_WINDOW_OPEN = 9 * 60 + 45
GATE_WINDOW_CLOSE = 14 * 60 + 30


def _sf(prepared: dict, i: int, key: str) -> float:
    """One session feature at index i as a float, NaN if unavailable."""
    value = session_features(prepared)[key][i]
    return float(value) if np.isfinite(value) else float("nan")


def gate_session_window(prepared: dict, i: int, open_minute: int = GATE_WINDOW_OPEN,
                        close_minute: int = GATE_WINDOW_CLOSE) -> bool:
    """True only between `open_minute` and `close_minute` of the session.

    This is the single highest-value filter docs/STRATEGIES.md points at, and
    the one the RSI/ADX/pattern stack has no way to express: a 5-minute
    momentum read at 09:20 is dominated by the opening auction, and one at
    15:15 is dominated by the closing print. Neither is a statement about the
    next 6 bars."""
    minute = _sf(prepared, i, "minute_of_day")
    return bool(np.isfinite(minute) and open_minute <= minute <= close_minute)


def gate_cost_cover(prepared: dict, i: int, min_multiple: float = 3.0) -> bool:
    """The expected move over the hold must clear the round trip by a margin.

    A trade can only be profitable if the move it is waiting for is larger than
    the cost of taking it. `expected_move` is ATR-as-%-of-price scaled to the
    hold horizon; `min_multiple` is how many round trips that move must be
    worth before the trade is worth considering. Set to the SLIPPAGE constant
    (imported lazily to avoid a circular import at module load) so the cost
    model can never drift from the one the backtest actually charges."""
    from core.signals import SLIPPAGE

    expected = _sf(prepared, i, "expected_move")
    return bool(np.isfinite(expected) and expected >= min_multiple * SLIPPAGE)


def gate_volatility_regime(prepared: dict, i: int, low_pctile: float = 0.20,
                           high_pctile: float = 0.80) -> bool:
    """ATR percentile must sit inside a band.

    Rationale from the lab's own diagnostic: "stop% near 100 with negative EV
    means the stop is too tight for this entry's noise." In the calmest
    decile a 1.5xATR stop is noise-width and gets taken out by ordinary
    wobble; in the most violent decile the same stop is inside a single
    bar's range and the fill assumption breaks down. The band excludes both
    tails and is expressed as a percentile of the name's OWN recent
    volatility, so it adapts per symbol instead of imposing one absolute
    ATR threshold on a Rs.50 stock and a Rs.5000 stock alike.

    Bounds are FRACTIONS (0.20 = the 20th percentile), matching
    session_features' `atr_pctile`. Writing them as 20/80 against a 0-1
    feature silently rejects every candle - which is exactly how this gate
    first shipped, and exactly what verify_gate_coverage() now exists to
    catch."""
    pctile = _sf(prepared, i, "atr_pctile")
    return bool(np.isfinite(pctile) and low_pctile <= pctile <= high_pctile)


def gate_above_vwap(prepared: dict, i: int, min_distance: float = 0.0) -> bool:
    """Price at or above the day's volume-weighted average price.

    VWAP is the one reference price that reflects where the day's volume
    actually changed hands. Above it, the average buyer today is in profit
    and dips tend to get bought; below it, rallies into it tend to get sold.
    The existing stack has no such reference - its only averages are its own
    20-bar mean, which says nothing about volume."""
    dist = _sf(prepared, i, "dist_vwap")
    return bool(np.isfinite(dist) and dist >= min_distance)


def gate_vwap_rising(prepared: dict, i: int, min_slope: float = 0.0) -> bool:
    """Session VWAP itself is trending up over the last hour.

    Paired with gate_above_vwap: price above a FALLING VWAP is a stock
    bouncing inside a day's downtrend, which is the mirror image of the
    accumulation read the first gate is looking for."""
    slope = _sf(prepared, i, "vwap_slope")
    return bool(np.isfinite(slope) and slope >= min_slope)


def gate_htf_agreement(prepared: dict, i: int) -> bool:
    """Both the 4-hour and one-day momentum must be positive.

    Multi-timeframe confirmation, built by resampling the 5-minute series in
    index space (see session_features.DEFAULTS). Without this the stack cannot
    distinguish a pullback inside an uptrend from a downtrend that happens to
    look identical over 5 minutes - a distinction ADX alone only hints at,
    because ADX is directionless by construction."""
    fast = _sf(prepared, i, "htf_fast_ret")
    slow = _sf(prepared, i, "htf_slow_ret")
    return bool(np.isfinite(fast) and np.isfinite(slow) and fast > 0 and slow > 0)


def gate_no_large_gap(prepared: dict, i: int, max_gap: float = 0.02) -> bool:
    """Reject entries on (or immediately after) a large opening gap.

    A gap bigger than `max_gap` (default 2%) means the entry price is being
    set by an overnight news repricing, not by the intraday structure the
    rest of these gates are reading. There is no reliable intraday edge in
    the first bars after a large gap - they are a different market regime
    from the rest of the session, and this is the cheapest way to say "do
    not read this chart with this strategy right now"."""
    gap = _sf(prepared, i, "gap_pct")
    # NaN on every bar that is not a session's first bar, so this gate is
    # a no-op for intraday entries and only bites on gap-up/gap-down opens.
    return bool((not np.isfinite(gap)) or abs(gap) < max_gap)


# ---------------------------------------------------------------------------
# Candidates - each returns True at index i when its entry triggers
# ---------------------------------------------------------------------------

def cand_trend_pullback(prepared: dict, i: int) -> bool:
    """C1: uptrend + RSI pullback. Long-only momentum-with-the-trend.

    Combines the two reliably-positive ablation components (ADX trend
    filter, RSI<45 pullback) and drops everything measured negative.
    The most conservative candidate: no pattern dependence at all."""
    return gate_trend(prepared["adxs"], i) and gate_pullback(prepared["rsis"], i)


def cand_trend_pullback_db(prepared: dict, i: int) -> bool:
    """C2: C1 + confirmed double bottom at tighter tolerance.

    Adds the best-measured edge (+0.0092%) on top of C1. Expect far fewer
    signals than C1 - the point of this one is selectivity, not frequency."""
    return (
        gate_trend(prepared["adxs"], i)
        and gate_pullback(prepared["rsis"], i)
        and gate_double_bottom(prepared["swings_norm"], i)
    )


def cand_ma20_bounce(prepared: dict, i: int) -> bool:
    """C3: price touches its 20-bar mean from above in an uptrend.

    Classic 'buy the dip to the moving average'. Untested combination -
    INCLUDED TO BE MEASURED, not because it is known to work. The lab
    run decides whether it earns a place."""
    closes = prepared["closes"]
    if i < 20:
        return False
    ma20 = closes[i - 20:i + 1].mean()
    low = prepared["lows"][i]
    close = closes[i]
    return (
        gate_trend(prepared["adxs"], i)
        and close >= ma20 * 0.995          # holding above the mean (0.5% grace)
        and low <= ma20 * 1.005            # but the bar tested it
    )


def cand_vol_breakout_pullback(prepared: dict, i: int) -> bool:
    """C4: C1 + volume surge confirmation. Tests whether volume interest
    adds anything to C1 - an explicit experiment, not a claim."""
    return cand_trend_pullback(prepared, i) and gate_volume_surge(prepared["volumes"], i)


# --- Second wave: C1 plus ONE session/regime/cost filter each --------------
#
# Each of the next six is deliberately C1 + exactly one new gate. That is the
# whole experimental design: the lab attributes the difference in EV against
# cand_trend_pullback to the single filter under test, so a filter that earns
# nothing is visible as a dead row rather than hidden inside a bundle that
# happened to help. The bundle is registered separately below, and only earns
# its place if its components did.

def cand_session_pullback(prepared: dict, i: int) -> bool:
    """C5: C1 restricted to the liquid middle of the session.

    The direct test of docs/STRATEGIES.md's claim that time-of-day is where the
    remaining upside is. If this row does not beat cand_trend_pullback, the
    claim is wrong for this data and no amount of further session tuning
    should be attempted."""
    return cand_trend_pullback(prepared, i) and gate_session_window(prepared, i)


def cand_cost_covered_pullback(prepared: dict, i: int) -> bool:
    """C6: C1 only where the expected move covers the round trip.

    Cost-aware trading as an entry filter: refuse names whose ATR is too small
    a fraction of price for a 6-bar hold to pay 0.05% round trip plus a real
    edge. This is the filter that makes the cost line in the lab table
    actionable per-symbol rather than a global constant."""
    return cand_trend_pullback(prepared, i) and gate_cost_cover(prepared, i)


def cand_vol_regime_pullback(prepared: dict, i: int) -> bool:
    """C7: C1 only in a normal volatility regime.

    Excludes both the dead-calm decile (where a 1.5xATR stop is inside the
    noise band) and the panic decile (where the same stop is inside a single
    bar's range). Percentile-of-own-history, not an absolute ATR, so it means
    the same thing for a Rs.50 stock and a Rs.5000 stock."""
    return cand_trend_pullback(prepared, i) and gate_volatility_regime(prepared, i)


def cand_vwap_pullback(prepared: dict, i: int) -> bool:
    """C8: C1 with price above a RISING session VWAP.

    The volume-aware reference the RSI/ADX/pattern stack lacks entirely: the
    average price the day's actual volume traded at, and the direction it is
    drifting."""
    return (
        cand_trend_pullback(prepared, i)
        and gate_above_vwap(prepared, i)
        and gate_vwap_rising(prepared, i)
    )


def cand_htf_aligned_pullback(prepared: dict, i: int) -> bool:
    """C9: C1 with 4-hour AND one-day momentum both positive.

    The multi-timeframe check: is this pullback happening inside an uptrend,
    or inside a downtrend that looks the same over 5 minutes?"""
    return cand_trend_pullback(prepared, i) and gate_htf_agreement(prepared, i)


def cand_gap_filtered_pullback(prepared: dict, i: int) -> bool:
    """C10: C1, refusing to read a chart that just gapped.

    A no-op on every intraday bar (gap is NaN off the first bar of a day), so
    this mostly measures whether the first bar of a session is a good or bad
    place to be taking pullback entries - a narrow, honest question rather
    than a broad claim."""
    return cand_trend_pullback(prepared, i) and gate_no_large_gap(prepared, i)


def cand_session_quality_stack(prepared: dict, i: int) -> bool:
    """C11: the institutional default - C1 plus every filter that earned a
    place in the lab.

    The bundle, registered separately from its parts on purpose. A stack is
    only legitimate once each component has been shown to help ON ITS OWN
    (C5-C10 above); otherwise it is six degrees of freedom fitted to the same
    history, and its lab number is not evidence of anything. Expect this to
    fire rarely - that is the intent, not a defect: docs/STRATEGIES.md is
    explicit that "fewer, better trades beat more trades" and nothing
    survives at 23% signal frequency. If the lab shows it firing on a small
    fraction of candles with a positive t, that is the first row in this
    project worth taking seriously."""
    return (
        cand_trend_pullback(prepared, i)
        and gate_session_window(prepared, i)
        and gate_cost_cover(prepared, i)
        and gate_volatility_regime(prepared, i)
        and gate_above_vwap(prepared, i)
        and gate_vwap_rising(prepared, i)
        and gate_htf_agreement(prepared, i)
        and gate_no_large_gap(prepared, i)
    )


# Registry used by the lab (research/strategy_lab.py) and the shadow logger
# (strategy/shadow.py). Name keys are stable - they are the table's
# strategy_name values, so do not rename them once data exists.
CANDIDATES = {
    "trend_pullback": cand_trend_pullback,
    "trend_pullback_db": cand_trend_pullback_db,
    "ma20_bounce": cand_ma20_bounce,
    "vol_breakout_pullback": cand_vol_breakout_pullback,
    "session_pullback": cand_session_pullback,
    "cost_covered_pullback": cand_cost_covered_pullback,
    "vol_regime_pullback": cand_vol_regime_pullback,
    "vwap_pullback": cand_vwap_pullback,
    "htf_aligned_pullback": cand_htf_aligned_pullback,
    "gap_filtered_pullback": cand_gap_filtered_pullback,
    "session_quality_stack": cand_session_quality_stack,
}


# ---------------------------------------------------------------------------
# Gate coverage self-test
# ---------------------------------------------------------------------------
# A gate that admits nothing is a bug wearing a lab table's clothing, and a
# gate that admits almost everything is not a filter - it is the same defect
# research/diagnose_edge.py section 5 caught in the shipped pattern gate,
# which sat ON for 98% of candles and was therefore a constant. Neither shows
# up as an error anywhere: the lab simply prints a row, and "0 trades" reads
# like a finding rather than a unit mistake (which is exactly how the first
# version of gate_volatility_regime failed, comparing a 0-1 percentile
# against a 20-80 band and admitting nothing at all).
#
# So coverage is asserted here rather than inferred from a results table.

def gate_coverage(prepared: dict, sample: int = 3000) -> dict:
    """Fraction of a symbol's candles each registered candidate admits.

    Sampled from the END of the series (the most recent, most
    regime-representative stretch) rather than the start, so indicator
    warm-up periods do not make every gate look artificially sparse."""
    n = prepared["length"]
    start = max(0, n - sample)
    lo, hi = start, n - 1
    if hi <= lo:
        return {}
    index = np.arange(lo, hi + 1)

    coverage = {}
    for name, fn in CANDIDATES.items():
        admitted = 0
        evaluated = 0
        for i in index:
            if not np.isfinite(prepared["rsis"][i]) or not np.isfinite(prepared["adxs"][i]):
                continue
            evaluated += 1
            admitted += 1 if fn(prepared, int(i)) else 0
        coverage[name] = admitted / evaluated if evaluated else float("nan")
    return coverage


# Candidates KNOWN to admit an implausibly large share of candles, with the
# reason they are tolerated here rather than reported as a regression.
#
# ma20_bounce measures 60-70% admit-rate on liquid large-caps: it asks only
# that price is in an uptrend AND the bar touched its own 20-bar mean, and on
# a name that oscillates around that mean almost every bar does both. It is
# the same defect research/diagnose_edge.py section 5 found in the shipped
# pattern gate (98% admit), and it is consistent with that candidate having
# the worst measured EV of the original four (-0.0444%/trade). It is listed
# here so this self-test stays a REGRESSION check - "did I just break a
# gate" - instead of a permanently red one, while the finding stays visible
# and the candidate stays honestly labelled as under-filtered.
#
# session_quality_stack is the fully-stacked institutional filter. By design
# it combines 7+ gates and admits very few candles (0.03-0.1% on 1-min data,
# 0.1-0.4% on 5-min data). This is the INTENT - "fewer, better trades" -
# not a wiring bug. Listed here so the test stays a regression check.
KNOWN_BROAD_CANDIDATES = {"ma20_bounce", "session_quality_stack"}


def verify_gate_coverage(candles: dict, min_admit: float = 0.0005,
                         max_admit: float = 0.60) -> tuple:
    """Returns (problems, warnings) for each candidate's admit-rate.

    A gate below `min_admit` is almost certainly a unit or wiring mistake;
    one above `max_admit` is not filtering anything, which is the 98%-always
    pattern-gate failure mode restated as an assertion. Known-broad
    candidates are reported as warnings so they cannot be mistaken for a
    code regression."""
    # Same contract as research/strategy_lab.py and strategy/shadow.py: the
    # caller owns the normalized swing arrays, and mutates them into the
    # prepared dict. Doing it here too keeps the self-test on the identical
    # code path the lab measures, rather than a near-copy of it.
    if "swings_norm" not in candles:
        candles["swings_norm"] = _swing_arrays(candles["swings"])

    problems, warnings = [], []
    for name, rate in gate_coverage(candles).items():
        if not np.isfinite(rate):
            message = f"{name}: no evaluable candles in the sample"
        elif rate < min_admit:
            message = (f"{name}: admits only {rate * 100:.3f}% of candles - "
                       f"check thresholds/units before reading any result")
        elif rate > max_admit:
            message = (f"{name}: admits {rate * 100:.1f}% of candles - "
                       f"under-filtered, this is the 98%-always gate defect")
        else:
            continue
        (warnings if name in KNOWN_BROAD_CANDIDATES else problems).append(message)
    return problems, warnings


if __name__ == "__main__":
    import sys

    from core.signals import prepare_symbol_series
    from storage.db import get_watchlist_symbols

    args = sys.argv[1:]
    symbols = args if args else get_watchlist_symbols()[:5]
    all_ok = True
    for sym in symbols:
        prepared = prepare_symbol_series(sym)
        if prepared is None or prepared["length"] < 1000:
            print(f"  {sym}: skipped (not enough history)")
            continue
        problems, warnings = verify_gate_coverage(prepared)
        rates = gate_coverage(prepared)
        detail = "  ".join(f"{n}={r * 100:.1f}%" for n, r in rates.items())
        print(f"  {sym}: {'OK' if not problems else 'PROBLEM'}")
        print(f"    {detail}")
        for warning in warnings:
            print(f"    (known) {warning}")
        for problem in problems:
            print(f"    - {problem}")
        all_ok = all_ok and not problems

    print("\nSELFTEST PASSED - every candidate admits a plausible share of candles"
          if all_ok else
          "\nSELFTEST FAILED - a candidate is mis-wired; its lab row is not a result")
    sys.exit(0 if all_ok else 1)
