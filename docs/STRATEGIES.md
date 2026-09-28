# Strategy candidates and promotion rules

How experimental strategies are added, measured, and — only if they earn it —
promoted. Read this before adding "a strategy that should make more profit":
every strategy here is a hypothesis with a number attached, and the numbers so
far say the hard part is costs, not idea generation.

---

## What exists now

`strategy/candidates.py` holds four candidate entries, each derived from a
specific measurement in [EDGE_ANALYSIS.md](EDGE_ANALYSIS.md) — the components
that measured positive — and deliberately excluding the ones that measured
negative (hammer/engulfing entries, the double-top-driven SELL leg):

| name | entry logic | built from |
|---|---|---|
| `trend_pullback` | ADX ≥ 20 **and** RSI < 45, long-only | the two reliably-positive ablation components (+0.0022% and +0.0006%, t = +3.9) |
| `trend_pullback_db` | `trend_pullback` **and** a confirmed double bottom at 1% tolerance | adds the best single measured edge (+0.0092%, t = +3.5) |
| `ma20_bounce` | uptrend, price dips to touch its 20-bar mean and holds above it | classic "buy the dip"; untested combination, included to be measured |
| `vol_breakout_pullback` | `trend_pullback` **and** volume ≥ 1.5× its 20-bar average | interest filter; untested, included to be measured |

All four are **long-only** — the measured loss concentrates in the SELL leg
(t = −4.9) and intraday drift favours longs.

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

## Measured results (40 symbols, 5-minute candles, Sept 2026)

At the default 1.5×ATR stop-only exit:

| strategy | trades | win% | EV/trade | t | EV w/target | EV late20 |
|---|---|---|---|---|---|---|
| baseline | 67,358 | 28.1 | −0.0361% | −10.3 | −0.0570% | −0.0705% |
| shipped BUY | 13,826 | 29.2 | −0.0386% | −5.1 | −0.0592% | −0.0731% |
| **`vol_breakout_pullback`** | 17,663 | 26.9 | **−0.0299%** | −4.2 | −0.0520% | −0.0695% |
| `trend_pullback` | 29,078 | 29.0 | −0.0358% | −6.8 | −0.0536% | −0.0629% |
| `trend_pullback_db` | 25,751 | 28.4 | −0.0403% | −7.6 | −0.0564% | −0.0734% |
| `ma20_bounce` | 43,214 | 28.0 | −0.0453% | −10.9 | −0.0649% | −0.0639% |

At a wider 3.0×ATR stop-only exit the ordering barely changes
(best: `vol_breakout_pullback` −0.0320%, baseline −0.0320%).

**Honest reading:** nothing here is profitable yet. Every candidate clears its
random-entry baseline slightly — `vol_breakout_pullback` beats baseline by
+0.006%/trade — but all sit far below the 0.05% cost line, and all t-stats are
negative. The candidates are *less bad* than the shipped signal, not *good*.
The shadow table is now accumulating the live out-of-sample record that decides
whether any of them earns promotion.

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

Until then: candidates log, nothing trades differently.

## Adding a candidate

1. Write a pure function `fn(prepared, i) -> bool` in
   `strategy/candidates.py` — arrays in, boolean out, no I/O. Reuse the gates
   (`gate_trend`, `gate_pullback`, `gate_double_bottom`, `gate_volume_surge`)
   where you can.
2. Register it in `CANDIDATES` with a stable name (it becomes a
   `strategy_signals.strategy_name` value — don't rename once data exists).
3. Run `python -m research.strategy_lab --n 60` and compare against baseline.
4. If it beats baseline *and* the shipped signal, keep it; the shadow logger
   picks it up automatically on the next cycle. If not, delete it — a dead
   hypothesis in a doc is worth more than a live one in the code.

## What would actually move the needle

Measured facts to keep in mind before adding more entries:

- **The exit dominates the entry.** Removing the take-profit gained ~+0.009%/trade
  — more than any entry filter's contribution. Exit variants are where the
  remaining upside is.
- **Costs are the wall.** 0.05% round-trip vs edges measured in thousandths of
  a percent. Fewer, better trades beat more trades; nothing survives at 23%
  signal frequency.
- **Frequency selection barely works intraday.** The best entry found anywhere
  (+0.0092% double-bottom longs) is still 5× under costs. If a real edge exists
  here, it is more likely in *when not to trade* (time-of-day, event filters,
  spread-aware cost models) than in another pattern gate.
