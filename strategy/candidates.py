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
"""
import numpy as np

from research.backtest import DEFAULT_PARAMS, SWING_ORDER

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


# Registry used by the lab (research/strategy_lab.py) and the shadow logger
# (strategy/shadow.py). Name keys are stable - they are the table's
# strategy_name values, so do not rename them once data exists.
CANDIDATES = {
    "trend_pullback": cand_trend_pullback,
    "trend_pullback_db": cand_trend_pullback_db,
    "ma20_bounce": cand_ma20_bounce,
    "vol_breakout_pullback": cand_vol_breakout_pullback,
}
