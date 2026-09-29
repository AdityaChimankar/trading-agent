# Edge analysis: the signal does not beat random entry

Measured with `research/diagnose_edge.py`. This document records what was
measured, what it means, and which changes are worth testing — so nobody
re-runs the experiment from scratch or, worse, tunes thresholds on a signal that
has nothing to tune.

Tool: `python -m research.diagnose_edge --n 120` (add `--exits` for §9)
Sample: 120 symbols, 2,201,290 candles, 6-candle horizon, **gross of costs**

---

## The headline

| | gross EV / trade | edge vs random | t |
|---|---|---|---|
| random entry (baseline long) | +0.0014% | — | +3.3 |
| **all signals** (direction-adjusted) | **−0.0042%** | −0.0042% | −4.5 |
| BUY signals (n = 117,349) | −0.0006% | −0.0020% | −0.4 |
| SELL signals (n = 389,039) | −0.0053% | −0.0040% | −4.9 |

Slippage in `research/backtest.SLIPPAGE` is **0.05% per trade** — an order of
magnitude larger than every edge measured anywhere below. The best edge found in
the entire ablation is +0.0092%, roughly **5× too small to survive costs**.

**BUY signals are statistically indistinguishable from a coin flip** (t = −0.4).
The rule is not a weak edge; it is not an edge.

### Method

The benchmark is the only honest one: **entering at a random candle**. If the
signals' average forward return does not beat the unconditional average forward
return over the same horizon, the entry rule adds nothing and no RSI/ADX tuning
can create the difference.

Everything is reported **gross of slippage** — slippage is a fixed additive drag,
so it cancels when comparing signal vs baseline. Seeing gross numbers is what
tells you whether *anything* is there before costs destroy it.

Each mean carries a t-statistic. With hundreds of thousands of candles almost
anything is "significant", so read the **t** as "distinguishable from noise" and
the **mean** as "would it survive costs".

---

## 1. Signal frequency: 23.0% of all candles fire

506,388 signals over 2,201,290 candles. A signal that fires on nearly a quarter
of all candles is not selecting anything — it is a description of the market
with a directional label attached.

## 2. The "pattern gate" is a constant, not a gate

| pattern code present on… | share of candles |
|---|---|
| bullish | 22.2% |
| bearish | 75.8% |
| **any** | **98.0%** |

This is the structural defect. `decision_agent.py` treats "pattern
confirmation" as one of three independent conditions, but a pattern code is
present on 98% of candles — so the gate passes essentially always while
structurally biasing the book **~76% short**. That bias is exactly why there are
389k SELL signals against 117k BUY, and why the losing side is the short side.

### The obvious fix — tighten the tolerance — does not work

`double_top` / `double_bottom` match when consecutive swing highs/lows are within
`tolerance`. Sweeping it:

| tolerance | gate ON | gross EV / trade |
|---|---|---|
| 1.5% (shipped) | 97.9% | −0.0051% |
| 1.0% | 95.5% | −0.0040% |
| 0.5% | 87.4% | −0.0042% |
| 0.3% | 78.6% | −0.0066% |

EV never turns positive. Tightening only makes the gate rarer, never more
selective in a *useful* way — because consecutive intraday swing points are
naturally within a few tenths of a percent of each other, so as implemented the
detector is structurally meaningless. It does not detect a double top; it detects
"two nearby swings".

## 3. Component ablation: which part carries the signal?

Long-perspective forward return of each gate, versus the unconditional long
baseline:

| component | edge vs baseline | t |
|---|---|---|
| `ADX >= 20` | +0.0006% | +3.9 |
| `RSI < 45` | +0.0022% | — |
| `RSI > 55` | +0.0018% | — |
| bullish pattern only | −0.0053% | −4.5 |
| bearish pattern only | +0.0013% | — |
| pattern bull **&** RSI < 45 | −0.0021% | — |
| pattern bear **&** RSI > 55 | +0.0036% | — |

Every combination is a rounding error next to a 0.05% cost. The one component
with a reliably positive t-stat (ADX) contributes +0.0006% — a fortieth of the
cost. Notably, the **bullish** pattern gate is the only gate that is actively
negative.

## 4. Pattern source split: the best and worst entries

| entry | edge | t |
|---|---|---|
| BUY via swing `double_bottom` | **+0.0092%** | +3.5 |
| BUY via `hammer` / `engulfing` | **−0.0083%** | −3.3 |
| SELL via `double_top` | −0.0034% | — |
| SELL via `engulfing` | −0.0101% | — |

The best edge found anywhere is the swing-based double bottom, and it is still
~5× smaller than slippage. The hammer/engulfing entries — the ones most likely
to be believed as "classic candlestick confirmation" — are the worst BUY source.

## 5. Horizon sweep: the edge decays immediately

Signal EV by holding period:

| hold | gross EV |
|---|---|
| 1 bar | +0.0003% |
| 2 bars | −0.0003% |
| 3 bars | −0.0013% |
| 6 bars | −0.0042% |
| 12 bars | −0.0143% |

Any hint of an effect exists only at 1–2 bars and is gone by 3. No holding
period is viable after costs — and 1-bar holds at 0.05% slippage is arithmetically
hopeless.

## 6. The exit logic traded live was never tested

`research/backtest.py` always exits after a **fixed 6 candles**. At the time of
this analysis the live agent used a **1.5×ATR stop with a 2:1 target**
(`core/position_sizing.py`). Those are different strategies, and only the
untested one was running.

Bar-by-bar walk-forward, stop winning intrabar ties (conservative):

| exit variant | net EV / trade | t |
|---|---|---|
| fixed 6-candle (what the backtest measures) | −0.0546% | −32.5 |
| 1.5×ATR stop + 2:1 target **(what ran live then)** | −0.0451% | −29.3 |
| **1.5×ATR stop, no target** | **−0.0363%** | −18.7 |
| 3.0×ATR stop + 2:1 target | −0.0519% | — |

Outcome mix for the stop+target variant: **stop 55.9% · target 23.4% ·
timeout 20.7%**.

Removing the take-profit is the **single largest measured improvement in this
entire analysis**: ~+0.009%/trade, roughly a 33% relative improvement, and it
costs nothing to implement. The target fires on fewer than a quarter of trades
while the stop fires on more than half — the ratio is cutting winners short in a
signal with no directional edge to begin with.

> **Applied.** `core/position_sizing.py` now defaults to `USE_TAKE_PROFIT =
> False`, and `research/strategy_lab.py` reproduces this gap independently on
> every candidate it measures. Still a smaller loss, not a profitable strategy.

---

## 7. Wave 2: the session / regime / cost filter layer (added later)

§6 established the exit as the dominant lever. The remaining open question was
the one [STRATEGIES.md](STRATEGIES.md) names as the only untested direction left:
*"if a real edge exists here, it is more likely in **when not to trade**
(time-of-day, event filters, spread-aware cost models) than in another pattern
gate."* Seven institutional-grade filters were built on top of the best
measured entry (`trend_pullback` = ADX ≥ 20 **and** RSI < 45), measured one at a
time against the same random-entry baseline, 60 symbols:

| filter added to `trend_pullback` | trades | EV/trade | vs baseline (−0.0319%) | EV late20 |
|---|---|---|---|---|
| session window 09:45–14:30 | 31,423 | −0.0388% | **worse** | −0.0380% |
| cost cover (≥3× round trip) | 43,924 | −0.0351% | worse (52 trades filtered) | −0.0505% |
| ATR percentile band 20–80 | 31,751 | −0.0412% | **worse** | −0.0567% |
| price above rising session VWAP | 3,192 | **+0.0166%** | **+0.049%** | −0.0415% |
| 4-hour + one-day momentum > 0 | 5,895 | −0.0447% | **worse** | −0.0714% |
| no ≥2% opening gap | 43,954 | −0.0354% | worse (22 trades filtered) | −0.0499% |
| all seven stacked | 405 | +0.0092% | +0.041% | **−0.1787%** |

**The layer did not produce an edge.** Three results are worth keeping:

1. **The VWAP filter is the only candidate in the project to clear the cost
   line** (+0.0166% net, a full round-trip cost above baseline) — and it fails
   three ways: t = +1.0 against a promotion bar of t ≥ 2; its late-20% EV is
   −0.0415% against +0.0323% in the early 80%, so the whole gain is in the older
   half of the history; and at a 3.0×ATR stop instead of 1.5×ATR it collapses to
   +0.0022% (t = +0.1). A result that exists at one stop width is a parameter
   coincidence. Hypothesis, not strategy.

2. **Cost-aware entry gating is arithmetically pointless at this cost level.**
   The expected move over the hold is ATR-%-of-price × √6; measured across
   369,751 candles on 20 symbols its median is **11.4× the round trip**
   (p5 = 5.6×, p95 = 27.1×), and only 0.009% of candles fall below even 1× cost.
   So the filter changed 52 trades out of 43,976. This closes off "trade only
   when the move can cover costs" as a direction: the signal is wrong about
   direction ~50% of the time, and no cost filter fixes a directional error.

3. **Multi-timeframe confirmation backfires at a 5-minute horizon.** Requiring
   the 4-hour *and* one-day trend to agree produced the worst result in the
   table (−0.0447%). The continuation being harvested is short-horizon, and HTF
   agreement filters precisely that away — a result worth having, because HTF
   confirmation is close to universal advice and it is wrong *here*.

The stacked variant shows the largest early-80 number of anything measured
(+0.0438%) and −0.1787% in the last fifth on 405 trades. Seven filters on one
history is seven degrees of freedom; that shape is what overfitting looks like,
and it is why each filter was measured alone before being stacked.

Full tables, admit-rates and the promotion rules are in
[STRATEGIES.md](STRATEGIES.md).

---

## Ranked recommendations

Ordered by measured evidence, best first.

1. **Remove the fixed take-profit.** Set `USE_TAKE_PROFIT = False` in
   `core/position_sizing.py` (it is already the default). Largest measured
   gain, smallest change, no new logic. Keep the 1.5×ATR stop. **Done.**
2. **Drop the SELL / short leg.** SELL carries the loss (t = −4.9) while BUY is
   indistinguishable from random (t = −0.4). The short bias is imposed by the
   pattern gate, not by evidence. **Done** — every candidate is long-only.
3. **Stop using double-top/bottom as a gate** until it requires a real
   intervening trough plus a minimum bar separation. As implemented it is on 98%
   of candles; §2 shows tightening the tolerance cannot rescue it.
4. **Drop `hammer` / `engulfing` entries.** Worst measured BUY source
   (−0.0083%, t = −3.3).
5. **Do NOT tune RSI/ADX thresholds.** With a 5× gap to costs, any apparent
   improvement from threshold search is fitting noise. `walk_forward_optimizer.py`
   will happily return a "best" parameter set here — that output should be
   treated as a warning, not a result.
6. **No holding period works.** 1 bar is the only non-negative horizon and it is
   +0.0003% against 0.05% costs.
7. **Do not expect entry filters to close the gap.** Seven of them were built
   and measured (§7); one cleared the cost line and it failed its own time-split.
   What remains untested, and is the only direction the evidence points at, is
   **execution cost**: every number here assumes a flat 0.05% round trip, with no
   spread, no impact, and no gap between signal and fill. Measuring realized cost
   per signal and trading only the cheapest decile of names attacks the actual
   binding constraint; nothing in the project does that yet.

## Caveats

- These are **in-sample** measurements across ~18k candles per symbol. They are
  strong evidence the signal lacks an edge; they are not proof of a specific
  alternative.
- The **exit variants have several degrees of freedom** (stop multiple, target
  ratio, intrabar tie-breaking, max bars). Removing the take-profit is the one
  change with a large margin, but it still needs walk-forward validation —
  and `research/walk_forward_optimizer.py` **cannot do it**, because it tunes
  entry thresholds only. Extending it to exits is the prerequisite for trusting
  recommendation 1 with real capital.
- Every number here is **gross** in the signal-vs-baseline sections and **net**
  in the exit section; the tool labels which is which.
- §7's filters are measured **in-sample** like everything else, on 60 symbols.
  The candidates are logging live to `strategy_signals` so the out-of-sample
  test the promotion rules require is accumulating independently.

## Reproducing

```bash
python -m research.diagnose_edge --n 120          # the tables above (default is 60 symbols)
python -m research.diagnose_edge --n 40 --exits   # adds the exit-variant section
python -m research.diagnose_edge --all            # whole watchlist
python -m research.diagnose_edge RELIANCE TCS     # specific symbols

python -m strategy.session_features               # §7 features: causality proof
python -m strategy.session_features --cost-cover  # §7: expected move vs the round trip
python -m strategy.candidates                     # §7 candidates: admit-rate proof
python -m research.strategy_lab --n 60            # §7 candidate EV table
```

Sections: 1 baseline · 2 signal vs baseline · 3 component ablation · 4
pattern-source split · 5 gate frequency · 6 horizon sweep · 7 tolerance sweep ·
8 exit variants (behind `--exits`).
