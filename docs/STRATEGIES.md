# Strategy candidates and promotion rules

How experimental strategies are added, measured, and — only if they earn it —
promoted. Read this before adding "a strategy that should make more profit":
every strategy here is a hypothesis with a number attached, and the numbers so
far say the hard part is costs, not idea generation.

**Note:** The enhanced ML model (`strategy/train_ml_model_v2.py`) now shows a
strong measured edge in walk-forward validation (+495% vs −135% for rule-based
across 10 symbols). It is tracked in the `ml_signals` table for out-of-sample
validation. See [ML_REDESIGN_SUMMARY.md](../ML_REDESIGN_SUMMARY.md) for details.

---

## What exists now

`strategy/candidates.py` holds the candidate entries, each derived from a
specific measurement in [EDGE_ANALYSIS.md](EDGE_ANALYSIS.md) — the components
that measured positive — and deliberately excluding the ones that measured
negative (hammer/engulfing entries, the double-top-driven SELL leg).

### Wave 1 — entry components

| name | entry logic | built from |
|---|---|---|
| `trend_pullback` | ADX ≥ 20 **and** RSI < 45, long-only | the two reliably-positive ablation components (+0.0022% and +0.0006%, t = +3.9) |
| `trend_pullback_db` | `trend_pullback` **and** a confirmed double bottom at 1% tolerance | adds the best single measured edge (+0.0092%, t = +3.5) |
| `ma20_bounce` | uptrend, price dips to touch its 20-bar mean and holds above it | classic "buy the dip"; under-filtered, see below |
| `vol_breakout_pullback` | `trend_pullback` **and** volume ≥ 1.5× its 20-bar average | interest filter |

### Wave 2 — session, regime and cost filters

Built to test [STRATEGIES.md's own conclusion](#what-would-actually-move-the-needle)
that "if a real edge exists here, it is more likely in *when not to trade*
than in another pattern gate". Each of the first six is `trend_pullback`
**plus exactly one** new gate, so the lab attributes any change in EV to the
single filter under test. The gates read
`strategy/session_features.py` (session VWAP, ATR percentile, higher-timeframe
momentum, opening gap, expected move).

| name | added gate | what it is testing |
|---|---|---|
| `session_pullback` | 09:45–14:30 only | the opening-auction and closing-auction chop |
| `cost_covered_pullback` | expected move ≥ 3× round trip | whether a cost-aware entry filter does anything |
| `vol_regime_pullback` | ATR percentile in 20–80 | calm-decile stop-hunting and panic-decile slippage |
| `vwap_pullback` | price above a **rising** session VWAP | the volume reference the RSI/ADX stack lacks |
| `htf_aligned_pullback` | 4-hour **and** one-day momentum > 0 | multi-timeframe confirmation |
| `gap_filtered_pullback` | no ≥2% opening gap | post-news-repricing bars |
| `session_quality_stack` | all of the above at once | the bundle, registered separately from its parts |

All candidates are **long-only** — the measured loss concentrates in the SELL
leg (t = −4.9) and intraday drift favours longs.

They are pure functions over prepared arrays: no DB, no state, no I/O. The lab
(`research/strategy_lab.py`) backtests them and the shadow logger
(`strategy/shadow.py`) evaluates them live — **the same code object**, so lab
and live can never disagree about what a strategy is.

## How they're measured

`python -m research.strategy_lab` runs every candidate plus three controls on
stored candles:

- **baseline** — a trade at *every* candle (the random-entry control; a
  candidate that can't beat this adds nothing)
- **shipped signal, BUY only** and **shipped signal, BUY+SELL** — the incumbent
  rule agent, for reference

Measurement discipline (each of these changes the numbers, so they're stated):

1. **Non-overlapping trades.** RSI < 45 persists for many candles; counting
   every one re-measures the same move dozens of times. After a trade exits,
   the next entry is allowed one bar later.
2. **1.5×ATR stop, stop wins intrabar ties** (conservative), max 24 bars.
   Both the stop-only exit and the 2:1-target exit are reported.
3. **Net of `research.backtest.SLIPPAGE`** (0.05% round trip).
4. **Time-split honesty check:** EV over the first 80% of history vs the last
   20%. A candidate that worked only early is curve-fit, not a strategy.

Cross-validation: the lab's baseline (−0.0361%/trade over 40 symbols) matches
`diagnose_edge.py`'s independent stop-only measurement (−0.0363%) to within
0.0002% — two separately written implementations agreeing, which is what makes
the rest of the table trustworthy.

## Verification before measurement

Two self-tests exist because a broken feature and a bad strategy look identical
in a results table — both print a number:

```
python -m strategy.session_features      # no feature depends on a future candle
python -m strategy.candidates            # no candidate is mis-wired or a no-op
python -m strategy.session_features --cost-cover   # the distribution behind the cost-gate result
```

`session_features` proves causality the only way that can be trusted on real
data: build every feature over a symbol's full history, build it again over a
truncated prefix, and require the values to be identical. Any centering,
full-series normalization or backfill changes its value at index *i* once later
candles exist, so it fails by construction. (It has already earned its keep:
this is how the first version of `bars_left_in_day`, which counted the day's
candles from a full-series group size and therefore knew the future of the day,
was caught.)

`candidates` asserts each candidate's **admit-rate**. A gate below 0.05% is
usually a unit mistake; a gate above 60% is not filtering anything — the same
defect `diagnose_edge.py` section 5 found in the shipped pattern gate, which sat
ON for 98% of candles. This has also earned its keep: `vol_regime_pullback`
shipped comparing a 0–1 percentile against a 20–80 band, admitted nothing, and
would otherwise have been read in the lab table as "this filter doesn't work"
rather than as the bug it was.

## Measured results (60 symbols, 5-minute candles, 2026)

At the default 1.5×ATR stop-only exit, all eleven candidates plus three
controls. EV/win%/t come from one `python -m research.strategy_lab --n 60` run
over 498-symbol/9.19M-candle data; the **admit%** column is the range observed by
`python -m strategy.candidates` on its own 4-symbol sample, so it is indicative
of how selective a gate is rather than measured on the same 60 symbols.

| strategy | trades | admit% | win% | EV/trade | t | EV w/target | EV early80 | EV late20 |
|---|---|---|---|---|---|---|---|---|
| baseline (every candle) | 101,986 | 100% | 28.1 | −0.0319% | −11.1 | −0.0565% | −0.0258% | −0.0564% |
| shipped signal, BUY only | 20,792 | | 29.1 | −0.0391% | −6.5 | −0.0610% | −0.0332% | −0.0616% |
| shipped signal, BUY+SELL | 57,105 | | 31.1 | −0.0345% | −9.8 | −0.0485% | −0.0353% | −0.0313% |
| `trend_pullback` | 43,976 | 30–37% | 29.0 | −0.0349% | −8.4 | −0.0552% | −0.0310% | −0.0498% |
| `trend_pullback_db` | 39,141 | 22–35% | 28.4 | −0.0379% | −8.9 | −0.0574% | −0.0334% | −0.0546% |
| `ma20_bounce` | 65,495 | 52–71% | 28.0 | −0.0410% | −12.2 | −0.0635% | −0.0375% | −0.0547% |
| `vol_breakout_pullback` | 26,574 | 2–8% | 26.9 | −0.0316% | −5.6 | −0.0529% | −0.0236% | −0.0612% |
| `session_pullback` | 31,423 | 13–42% | 30.8 | −0.0388% | −8.6 | −0.0600% | −0.0390% | −0.0380% |
| `cost_covered_pullback` | 43,924 | 30–37% | 29.0 | −0.0351% | −8.4 | −0.0553% | −0.0311% | −0.0505% |
| `vol_regime_pullback` | 31,751 | 8–32% | 28.0 | −0.0412% | −8.7 | −0.0610% | −0.0372% | −0.0567% |
| **`vwap_pullback`** | **3,192** | **0.8–1.9%** | 28.5 | **+0.0166%** | **+1.0** | −0.0167% | +0.0323% | **−0.0415%** |
| `htf_aligned_pullback` | 5,895 | 0–3% | 28.8 | −0.0447% | −4.1 | −0.0632% | −0.0390% | −0.0714% |
| `gap_filtered_pullback` | 43,954 | 30–37% | 29.0 | −0.0354% | −8.5 | −0.0556% | −0.0316% | −0.0499% |
| `session_quality_stack` | 405 | 0.1–0.4% | 26.7 | +0.0092% | +0.2 | −0.0216% | +0.0438% | −0.1787% |

### What this actually says

**One filter beat the baseline, and it does not survive its own honesty check.**
`vwap_pullback` returns +0.0166%/trade net against a random-entry baseline of
−0.0319% — a +0.049%/trade margin, which is a full round-trip cost, and the only
candidate in the project to clear the cost line at all. It is also the most
selective candidate by construction (0.8–1.9% of candles).

But `t = +1.0` fails the promotion bar of t ≥ 2, and more damningly **EV late20
is −0.0415% against +0.0323% early80** — the entire gain sits in the earlier
history and the sign inverts in the recent fifth. That is the signature the
time-split exists to catch.

It is also **exit-parameter dependent**: at a 3.0×ATR stop the same candidate
returns +0.0022% (t = +0.1), and `session_quality_stack` goes to −0.0559%. A
result that only exists at one stop width is a parameter coincidence, not an
edge. It is a hypothesis worth out-of-sample testing, not a strategy, and it is
not promoted.

**The bundle is the clearest curve-fit in the table.** `session_quality_stack`
shows the largest early-80 number of anything measured here (+0.0438%) and
−0.1787% in the last fifth, on 405 trades — below the 500-trade minimum. Seven
filters stacked on one history is seven degrees of freedom; an in-sample
improvement from that shape is what overfitting *looks like*, and it is the
reason each filter was measured alone first.

**Cost-aware entry gating is provably a no-op on this data.**
`cost_covered_pullback` filtered 52 trades out of 43,976. The reason is
arithmetic, not bad luck. The expected move over the hold is ATR-%-of-price × √6;
measured across 369,751 candles on 20 symbols its median is **11.4× the round
trip** (p5 = 5.6×, p95 = 27.1×), and only 0.009% of candles fall below even 1×
cost. "Only trade when the move can cover costs" is the right idea and a useless
filter at this cost level and this volatility — the edge is missing because the
direction is wrong ~50% of the time, not because the move is too small to pay for.

**Three filters made it actively worse.** Session windowing (−0.0388%),
volatility-regime banding (−0.0412%) and HTF alignment (−0.0447%) all lost to
the unfiltered `trend_pullback` and to the random-entry baseline. The
higher-timeframe result is the most interesting: requiring that a 4-hour *and*
one-day trend agree selects the *worst* subset. On 5-minute data the strongest
continuation is short-horizon, and multi-timeframe confirmation filters exactly
that away.

**`ma20_bounce` is the shipped pattern-gate defect, reproduced.** It admits
52–71% of candles — on a liquid large-cap, price is in an uptrend and testing
its own 20-bar mean on most bars. It has the worst EV in the table (−0.0410%),
consistent with a gate that is not gating.

**Honest summary: the institutional filter layer did not produce a measured
edge.** Seven filters, 4.2M extra candles, one candidate above the cost line,
and it fails t and fails the time-split. The premise that a better filter stack
creates an edge on 5-minute Indian equities at 0.05% round-trip cost is not
supported by this data. That is a real result, and it is worth more than a
table of flattering candidates would have been.

## What did get fixed

The one lever with large, consistent, independently-reproduced evidence: **the
exit**.

| exit | net EV/trade |
|---|---|
| 1.5×ATR stop + 2:1 take-profit (was live) | −0.0451% |
| 1.5×ATR stop, no take-profit | **−0.0363%** |

`core/position_sizing.py` now defaults to `USE_TAKE_PROFIT = False`, and the
lab reproduces the gap independently on every candidate — `vwap_pullback` is
+0.0166% stop-only and −0.0167% with the target, so the target flips a positive
number negative. A 2:1 target on a 1.5×ATR stop needs a ~3×ATR move inside a
24-bar hold, which almost never happens: the target mostly caps small favourable
drifts while the stop still takes the full loss on the rest. This is still a
smaller loss, not a profitable strategy.

Also applied: **volatility-aware risk scaling** — position size scales down when
a name's ATR sits in the top of its own recent range, because that is when a
1.5×ATR stop falls inside one bar and planned risk stops being realisable risk.
It can only reduce risk, never increase it, and it reuses the same
`session_features` percentile the gates read so sizing and entry cannot
disagree about what "high volatility" means.

## Promotion rules

A candidate may replace or gate the live agent only when **all** of these hold
on data it has *not* influenced:

1. **Sample:** ≥ 500 non-overlapping trades in the evaluation window.
2. **Net edge:** EV/trade > 0 net of slippage, t ≥ 2.
3. **Out-of-sample:** the same holds in the shadow table's live record
   (3+ weeks), not just in the lab's in-sample run.
4. **No regression:** it beats the shipped signal's own EV over the same
   window, not just the baseline.
5. **Stability:** EV in the worst third of trading days stays > 0
   (a strategy that only makes money on quiet days is a liability).
6. **Time-split:** EV in the most recent 20% of history stays > 0. This is
   listed separately from rule 4 because it is the one that killed
   `vwap_pullback` and `session_quality_stack` above, and a rule that only
   exists inside a longer list is a rule that gets skipped.

Until then: candidates log, nothing trades differently.

## Adding a candidate

1. Write a pure function `fn(prepared, i) -> bool` in
   `strategy/candidates.py` — arrays in, boolean out, no I/O. Reuse the gates
   (`gate_trend`, `gate_pullback`, `gate_double_bottom`, `gate_volume_surge`,
   and the wave-2 session/regime gates) where you can. If it needs a new
   feature, add it to `strategy/session_features.py` and extend
   `verify_causal()` to cover it — a feature that is not covered by that test
   is a feature nobody has proven cannot see the future.
2. Register it in `CANDIDATES` with a stable name (it becomes a
   `strategy_signals.strategy_name` value — don't rename once data exists).
3. Run `python -m strategy.candidates` (admit-rate) and
   `python -m strategy.session_features` (causality) before trusting anything.
4. Run `python -m research.strategy_lab --n 60` and compare against baseline
   **and against `trend_pullback`**, since every new filter is measured on top
   of it.
5. If it beats baseline *and* `trend_pullback` *and* survives the late-20%
   check, keep it; the shadow logger picks it up automatically on the next
   cycle. If not, delete it — a dead hypothesis in a doc is worth more than a
   live one in the code.

## What would actually move the needle

Measured facts to keep in mind before adding more entries:

- **The exit dominates the entry.** Removing the take-profit gained ~+0.009%/trade
  — more than any entry filter's contribution, and the only change so far that
  held up across every candidate and every exit variant tested. Exit variants
  are where the remaining upside is.
- **Costs are not the binding constraint; direction is.** Expected move exceeds
  the 0.05% round trip by 3–50× on every name measured, so no cost-aware entry
  filter can help. The signal is wrong about half the time, and a filter can
  only remove trades, not fix the ones it keeps.
- **Frequency selection does not work intraday.** Eleven candidates, one above
  the cost line, and it fails the time-split. The best entry found anywhere
  (+0.0092% double-bottom longs) is still 5× under costs.
- **Multi-timeframe confirmation backfires at this horizon.** Requiring 4-hour
  and daily agreement produced the worst result in the table. Trend-following on
  5-minute bars lives in the short-horizon move, which HTF filters remove.
- **The untested direction is execution, not signal generation.** Every number
  here assumes 0.05% round trip. Real cost includes spread, impact and the gap
  between signal and fill; an agent that measured *realized* cost per signal
  and traded only the cheapest decile of names would be attacking the actual
  binding constraint. Nothing in the project does that yet, and it is the one
  direction the evidence actually points at.

