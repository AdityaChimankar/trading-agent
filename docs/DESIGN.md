# Design decisions

Why the code is built this way. Most of these choices were forced by a concrete
bug — the bug is recorded with the decision, because the decision looks
arbitrary without it.

---

## Storage and concurrency

### The `watchlist` table is the single source of truth

Everything reads symbols via `storage/db.py`'s `get_watchlist()` /
`get_watchlist_symbols()`. No module keeps its own list.

**The bug:** a hardcoded list once meant signals silently ran against 2 symbols
after the watchlist had already been set up with 500. Every file that needs
symbols or tokens must ask the DB.

### WAL mode, a 30 s busy timeout, and per-symbol commits

`storage/db.py` opens every connection with `journal_mode=WAL`,
`synchronous=NORMAL`, `temp_store=MEMORY`, and `busy_timeout=30000`. SQLite's
default mode allows one writer with no wait, but during market hours four
processes write: `ingest/live_ticker.py`'s flush thread, `scripts/scheduler.py`,
and manual CLI runs.

On top of that, **every write loop commits per symbol** rather than once at the
end of a watchlist scan. Holding one write transaction open across hundreds of
symbols — with real network-latency LLM calls in between — can exceed even a
30 s timeout and produce "database is locked" in another process. That is why
`strategy/llm_decision_agent.py` commits as it goes.

### `init_db()` is idempotent and self-healing, and runs on API startup

`CREATE TABLE IF NOT EXISTS` is a no-op on a table that already exists, so a
`schema.sql` change that adds a column does **not** apply to a database someone
already created. `storage/db.py` runs the schema and then applies a small
explicit migration list (`_migrate_add_missing_columns`), ignoring
"duplicate column" errors.

**The bug:** a real `no column named stop_loss` crash, caused by adding
`stop_loss`/`take_profit` to `open_positions` and relying on remembering to
re-run the initializer by hand. `api/main.py` now calls `init_db()` on every
startup, which closes that gap permanently.

### No relative file paths

`paths.py` resolves every file location from `__file__`. Files used to be opened
by bare name (`.access_token`, `symbols.txt`, `instruments.csv`,
`watchlist_resolved.py`), which silently resolved against the *current working
directory* — so a run from anywhere but the repo root either failed or wrote a
fresh file somewhere unexpected. `ingest/instrument_lookup.py` used to write
`watchlist_resolved.py` into whatever directory you happened to be in.

---

## Ingestion

### `live_ticker.py` never touches the database from its tick callback

`on_ticks` does **only** in-memory dict updates. A separate background thread
flushes completed minute-buckets every 5 s over one persistent connection.

**The bug:** Kite's own guidance is that the WebSocket is dropped if the
callback blocks on calculation or I/O — the server can't tell the client is alive
if it isn't consuming ticks fast enough. Opening a connection and committing
inside `on_ticks` at hundreds of symbols caused repeated 1006 disconnects.

Two related details: the mode auto-switches from `MODE_FULL` to the lighter
`MODE_QUOTE` above `LARGE_WATCHLIST_THRESHOLD` symbols (we only ever use
last price/volume/OHLC, never order-book depth), and the flush uses the latest
**exchange** minute reported by the ticks rather than the machine's local clock,
so a skewed or non-IST machine clock can't bucket candles incorrectly.

### News comes from RSS because Kite Connect has no news API

`ingest/fetch_news.py` polls nine free feeds (Moneycontrol, Economic Times,
CNBC-TV18, Livemint ×2, Business Standard ×2, NDTV Profit, and BSE's
`announcements.xml`) and
keyword-matches items to watchlist symbols. `analysis/sentiment.py` then scores
them with Gemini.

### `fetch_historical.py` is resumable by design

Kite caps how many days one historical request may span, and rate-limits to
roughly 3 requests/second. The backfill chunks requests per interval, throttles
to ~2.5 req/s, retries on rate-limit errors, and skips symbols already fully
backfilled — so a 500-symbol run that dies partway can simply be re-run.

---

## The three decision paths

### `decision_agent.py` is the tested path; the other two are comparisons

`strategy/decision_agent.py`'s `evaluate_signals()` is what
`research/backtest.py` and `research/walk_forward_optimizer.py` validate.

`llm_decision_agent.py` and `ml_decision_agent.py` run **alongside** it, logging
the rule-based call at the same moment, and write to *separate* `llm_signals` /
`ml_signals` tables. Neither feeds the rule-based decision, and neither is a
candidate replacement until its agreement rate and real outcomes justify it.
Separate tables make that comparison impossible to contaminate.

### Thresholds are parameters with the tested values as defaults

`evaluate_signals(rsi_oversold=30, rsi_overbought=70, adx_threshold=20, ...)` —
the defaults are the values the original backtest used. Only
`walk_forward_optimizer.py` passes non-default values, so normal live runs are
unaffected by the optimizer's activity.

### ML features are checked for train/serve skew

`strategy/ml_decision_agent.py`'s live feature construction was verified against
`strategy/train_ml_model.py`'s training-time construction and confirmed
identical. A real skew issue surfaced during that check — the two initially
disagreed because they were compared at different candle indices, not because
the feature logic differed — which is exactly the failure mode that silently
produces bad predictions if left unchecked.

### Timestamps come from the source candle, not the wall clock

`core/quant_indicators.indicators_from_df()` returns the candle's `timestamp`,
and `decide()` exposes it as `signal_timestamp`. All three agents log signals
against it. Logging `datetime.now()` instead meant a signal computed from a
candle that had already closed was stamped as if it were new — which made
signals impossible to join back to candles, and made the dashboard's chart
markers never line up.

---

## Analysis layer

### `rankings.py` and `technical_summary.py` are comparison views, not signals

Neither is backtested or walk-forward validated, and both say so in their
docstrings. `decision_agent.py` remains the trading signal.
`opportunity_finder.py` ranks by conviction (ML/LLM/sentiment weights) — a
different question from "is this tradeable after costs".

### Sentiment is neutral during backtesting

Historical news is not backfilled candle-by-candle the way price is, so
`research/backtest.py`, `research/walk_forward_optimizer.py` and
`strategy/train_ml_model.py` all pass `NEUTRAL_SENTIMENT = 0.0`. All three
currently validate the RSI/ADX/pattern logic in isolation. Once the live
pipeline has run for a few weeks, real historical sentiment will exist for
future work.

---

## Risk

### `position_sizing.py` only ever produces a plan

Risk-per-trade, the ATR stop distance, the reward:risk ratio and a hard
max-position-% cap are all tunable constants at the top of the file. Nothing in
it places an order.

### `portfolio_risk.py` sizes against what is already open — three checks

1. **Total risk budget** across all simultaneously open positions (default 6% of
   capital).
2. **Same-symbol concentration.** Closes a real gap found in testing: the
   correlation check correctly excludes a symbol from being "correlated with
   itself", which meant nothing prevented accumulating multiple positions in the
   *same* stock. Reproduced concretely — combined exposure reached **37.9%** of
   capital, well past the intended 20% cap — before fixing it.
3. **Correlation-cluster cap** (default 20%) across *different* positions that
   move together, measured from real historical candle returns rather than an
   assumed sector label, so it catches co-movement a sector classification
   would miss.

All three were verified against synthetic data in deliberately correlated,
independent and concentrated scenarios.

### `position_monitor.py` checks exit conditions in a fixed order

stop-loss hit → take-profit hit → the rule-based signal reversed → else HOLD.
It only computes a recommendation; closing a position is still a manual click.
Every branch (long/short, profit and loss, both exit triggers, signal reversal)
was individually tested with known price/level combinations first.

> Note: `research/diagnose_edge.py` later showed the take-profit is actively
> harmful here — the stop fires on 55.9% of trades and the 2:1 target on only
> 23.4%. See [EDGE_ANALYSIS.md](EDGE_ANALYSIS.md).

---

## Validation

### Transaction costs live in exactly one place

`research/backtest.SLIPPAGE` (0.05%) is deducted from every simulated trade, and
`strategy/ml_features.py`'s labels use the same constant so training and
backtesting agree.

**The bug:** the value was a literal `0.0005` duplicated in three files, and the
entry-price convention (next candle's open + slippage) had already silently
drifted between `backtest.py` and `walk_forward_optimizer.py` — the two
implementations disagreed about what a fill was. Both now import the shared
definition from `research/backtest.py`. Live P&L in `position_monitor.py` still
values open positions at the raw last price, so its numbers remain slightly
optimistic versus a real fill.

### The vectorized fast path is verified against the slow loop

`research/walk_forward_optimizer.py` evaluates parameter grids with NumPy array
operations instead of a per-candle Python loop — this is what makes 500-symbol
optimization take under a minute instead of hours. It was verified to produce
identical results to the loop-based logic across 135 cases before being trusted,
and `--selftest` re-verifies that on your own data:

```bash
python -m research.walk_forward_optimizer --selftest 360ONE   # 27/27 combos matched
python -m research.backtest --selftest                        # fast path == reference
```

### `--selftest` exists because "verified on synthetic data" is not a guarantee

Both self-tests compare the fast path against a straightforward reference
implementation on *your stored candles*, not on fixtures. `MAX_WORKERS = 4` is a
deliberate cap rather than `os.cpu_count()`: spawning many processes that each
reload scipy's compiled libraries at once can exceed Windows' default page file
and crash with a DLL load error.

### `diagnose_edge.py` exists because the backtest couldn't answer the real question

`backtest.py` tells you the strategy loses money. It cannot tell you whether
that is a cost problem (trade more selectively) or a no-edge problem (no
threshold tuning can help) — and those demand opposite responses. The diagnostics
tool measures the signal against a random-entry baseline and ablates each
component. It also exposed that the exit logic traded live had never been tested
at all: the backtest always exits at a fixed 6 candles, while the live agent uses
a 1.5×ATR stop with a 2:1 target.

---

## Known gaps

- Access-token refresh is manual daily; a TOTP auto-login would remove that step.
- `live_ticker.py` needs a static IP registered with Zerodha only if order
  placement is ever added (not needed for data-only paper trading).
- `portfolio_risk.py`'s 500-candle correlation lookback and its thresholds are
  fixed constants, not re-validated the way the decision thresholds are. They
  deserve the same rigor.
- The ML model uses scikit-learn's `HistGradientBoostingClassifier`, not
  LightGBM/XGBoost — a deliberate substitution (same histogram-based gradient
  boosting family) made because the development environment could not install
  the other two. No model versioning or drift detection: each training run
  overwrites `models/ml_model.joblib`.
- `walk_forward_optimizer.py` tunes **entry thresholds only** and therefore
  cannot validate the exit logic, which is where the largest measured gain is.
- `ingest/live_ticker.py` is the only module that cannot be exercised offline —
  it opens a live WebSocket at import — so it is compile-checked and reviewed
  only. Watch its terminal output during market hours.
- SEBI algo-trading disclosure rules apply once this moves from personal signals
  to automated order placement. Out of scope for this POC.
