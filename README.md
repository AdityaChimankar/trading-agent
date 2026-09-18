# Intraday Trading Signal Agent

A local-first AI agent that combines real-time market data, deterministic
technical/pattern analysis, LLM-based sentiment scoring, and an ML model
to generate intraday BUY/SELL/HOLD signals for NSE stocks, with ATR-based
position sizing, portfolio-level risk checks, walk-forward parameter
validation, and live position monitoring. Paper-trading signals only -
no live order execution.

## Project structure

```
trading-agent/
├── README.md                        This file
├── requirements.txt                  All Python dependencies
├── .env.example                       Credential template (Kite + Gemini keys)
├── schema.sql                         SQLite schema
├── db.py                              DB connection (WAL mode) + watchlist single source of
│                                       truth + safe schema migrations
│
├── build_nifty500_symbols.py          Downloads current Nifty 500 list from NSE -> symbols.txt
├── instrument_lookup.py               Resolves symbols -> Kite instrument tokens -> watchlist_resolved.py
├── kite_auth.py                       Daily Zerodha login, generates access token
├── validate_setup.py                   Pre-flight check: Kite auth/data, news feeds, Gemini API, watchlist
├── fetch_historical.py                Chunked, resumable, rate-limit-safe historical backfill
├── live_ticker.py                     WebSocket live candle streaming (background-thread DB flush)
├── fetch_news.py                       Free RSS news polling, keyword-tagged to symbols
│
├── quant_indicators.py                RSI, ATR, ADX - deterministic, no LLM
├── pattern_detection.py               Candlestick + swing patterns
├── sentiment.py                        Gemini sentiment scoring on tagged news
├── decision_agent.py                  Rule-based BUY/SELL/HOLD (tested, backtestable, tunable thresholds)
├── llm_decision_agent.py              LLM-based decision agent - runs PARALLEL for comparison
├── ml_features.py                      Feature engineering for the ML model (no lookahead, verified)
├── train_ml_model.py                   Trains the ML model (chronological train/test split)
├── ml_decision_agent.py                ML-based decision agent - runs PARALLEL for comparison
├── digest.py                           End-of-day narrative summary
├── rankings.py                         Watchlist-wide bullish/bearish scoring (dashboard sidebar)
├── position_sizing.py                  ATR-based position sizing + stop-loss/take-profit
├── portfolio_risk.py                    Portfolio-level risk: same-symbol + correlation clusters +
│                                        total risk budget
├── position_monitor.py                 Live P&L + HOLD/SELL indicator for open positions
│
├── backtest.py                         Vectorized backtesting harness (single/multi-symbol/--all)
├── walk_forward_optimizer.py          Rolling train/test parameter re-fitting, out-of-sample
│                                        validated, parallelized (capped workers) + --selftest
├── scheduler.py                        Automation: news -> sentiment -> decisions -> ML compare
│                                        -> LLM compare -> digest
├── dashboard.py                        Streamlit dashboard: multi-panel chart, sidebar rankings,
│                                        Position Monitor panel, portfolio-aware position sizing
│
└── data/
    └── trading_agent.db                SQLite database (created/migrated by db.py)
```

## Tech stack & cost

| Layer | Choice | Cost |
|---|---|---|
| Market data + news | Zerodha Kite Connect | ~Rs.500/month |
| Watchlist sourcing | NSE Nifty 500 CSV + `requests` | Rs.0 |
| Quant + patterns | pandas, numpy, scipy | Rs.0 |
| Sentiment + LLM decisions | Gemini Flash-Lite API | ~Rs.400-1200/month |
| ML model | scikit-learn (HistGradientBoostingClassifier) + joblib | Rs.0 |
| Orchestration | plain Python + APScheduler | Rs.0 |
| Storage | SQLite (WAL mode) | Rs.0 |
| Dashboard | Streamlit + Plotly | Rs.0 |

---

## Phase 1: One-time setup

```bash
pip install -r requirements.txt
```

1. Sign up at https://developers.kite.trade/signup and subscribe to the
   paid Kite Connect plan (required for live + historical data).
2. Get a free Gemini API key at https://ai.google.dev.
3. Copy `.env.example` to `.env` and fill in `KITE_API_KEY`,
   `KITE_API_SECRET`, and `GEMINI_API_KEY`.
4. Build your watchlist:
   ```bash
   python build_nifty500_symbols.py         # -> symbols.txt (or write your own list)
   python instrument_lookup.py symbols.txt   # -> watchlist_resolved.py
   ```
5. Initialize the database:
   ```bash
   python db.py
   ```

## Phase 2: Historical backfill (one-time, or occasional top-up)

```bash
python kite_auth.py          # daily login - opens a URL, paste back the request_token
python fetch_historical.py   # backfills ~1 year of 5-min candles, resumable if interrupted
```

For 500 symbols, expect the backfill itself to take 30-60 minutes due to
Kite's per-request rate limit - this is a one-time cost, re-runs skip
symbols already backfilled.

## Phase 3: Validate before trusting anything live

```bash
python backtest.py --all
python walk_forward_optimizer.py --all
python walk_forward_optimizer.py --selftest      # optional: verify the fast path on YOUR data
python train_ml_model.py --all                    # optional: train the ML comparison model
```

`backtest.py` checks win rate, risk-reward ratio, and max drawdown with
fixed default thresholds. `walk_forward_optimizer.py` goes further - it
re-fits RSI/ADX thresholds per stock on rolling training windows and
validates on genuinely out-of-sample data it never saw during tuning.
`--selftest` independently confirms its vectorized fast path matches the
slow per-candle loop on your actual market data (not just the synthetic
data it was verified against during development). `train_ml_model.py`
trains the optional ML comparison model with a strict chronological
train/test split and reports both classification accuracy and an actual
trade simulation. Don't skip any of this - it's what tells you whether
the rule-based logic has an edge before it runs live.

## Phase 4: Daily routine (every trading day, 9:15 AM - 3:30 PM IST)

```bash
python kite_auth.py          # 1. refresh today's access token (manual, ~1 min, required daily)
python validate_setup.py     # 2. pre-flight check - confirms Kite, news feeds, and Gemini are all working
```

`validate_setup.py` exits with code 0 if everything passes, 1 if not -
catches a dead API key or expired token here, not 5 minutes into market
hours. You can chain it: `python validate_setup.py && python live_ticker.py`
will refuse to start if a check fails.

Then, in separate terminals:
```bash
python live_ticker.py        # 3. streams live candles all day
python scheduler.py          # 4. runs news / sentiment / decisions (5 min) / ML compare (5 min) /
                              #    LLM compare (15 min) / digest (3:35 PM)
streamlit run dashboard.py   # 5. optional - open http://localhost:8501 to watch live,
                              #    manage open positions, and monitor P&L
```

At market close (3:30 PM), Ctrl+C both `live_ticker.py` and `scheduler.py` -
no cleanup needed.

## Design notes worth knowing

- **The watchlist has ONE source of truth: the `watchlist` DB table**,
  read via `db.get_watchlist()` / `get_watchlist_symbols()`. Every file
  reads from there - none hardcode their own symbol list. That was
  exactly the bug that once caused signals to silently run on only 2
  symbols after a 500-symbol watchlist was already set up.
- **`decision_agent.py` is the tested path.** Its `evaluate_signals()` rule
  logic is what `backtest.py` and `walk_forward_optimizer.py` validate.
  Thresholds are parameterized (not hardcoded) with defaults matching the
  originally tested values, so only the optimizer passes non-default
  values - normal live runs are unaffected. `llm_decision_agent.py` and
  `ml_decision_agent.py` both run alongside for comparison, writing to
  *separate* `llm_signals` / `ml_signals` tables - neither feeds into the
  rule-based decision, and neither is a replacement until its agreement
  rate + real outcomes justify that.
- **`ml_decision_agent.py`'s live feature-building was checked against
  `train_ml_model.py`'s training-time feature-building and confirmed
  identical** (a real train/serve skew bug was caught and fixed during
  development - the two initially disagreed because they were compared
  at different candle indices, not because the logic itself was wrong,
  but it's exactly the kind of mismatch that silently produces bad
  predictions if it isn't checked).
- **SQLite runs in WAL mode with a 30s busy_timeout** (set in
  `db.py`'s `get_connection()`), since `live_ticker.py`, `scheduler.py`,
  and `dashboard.py` all access the DB concurrently. On top of that,
  every write loop (decision cycles, LLM/ML comparison cycles) commits
  **per-symbol**, not once at the end of a whole watchlist scan -
  holding one write transaction open across hundreds of symbols
  (especially with real network-latency LLM calls in between) can
  exceed even a generous busy_timeout and cause "database is locked"
  errors in other processes trying to write at the same time.
- **`db.py`'s `init_db()` is idempotent and self-healing, and
  `dashboard.py` calls it on every startup.** `CREATE TABLE IF NOT
  EXISTS` alone won't add a new column to a table that already exists
  on your machine - a schema change (like adding `stop_loss`/
  `take_profit` to `open_positions`) only takes effect once something
  actually runs the migration. Relying on remembering to manually
  re-run `python db.py` after every update is exactly what caused a
  real `no column named stop_loss` crash during development - calling
  `init_db()` from `dashboard.py` itself closes that gap for good.
- **`live_ticker.py`'s tick callback (`on_ticks`) does ONLY in-memory
  dict updates - no DB access at all.** All SQLite writes happen in a
  separate background thread that flushes completed minute-buckets
  every few seconds. Kite's own guidance is that the WebSocket
  connection gets dropped if `on_ticks` blocks on calculation or I/O -
  doing DB writes inside that callback at scale (hundreds of symbols)
  was the root cause of repeated 1006 disconnects.
- **Kite Connect has no news API.** News comes from free RSS feeds
  (Moneycontrol, Economic Times), keyword-matched to your watchlist.
- **Backtest, walk-forward, and ML training sentiment is neutral
  (0.0)/unused.** Historical news isn't backfilled candle-by-candle the
  way price is, so all three currently validate the RSI/ADX/pattern
  logic in isolation. Once you've run the live pipeline for a few
  weeks, real historical sentiment will exist for future work.
- **No transaction costs/slippage modeled** anywhere - real fills will
  be slightly worse than the raw numbers shown.
- **`validate_setup.py` checks the REST APIs (Kite quotes, news feeds,
  Gemini), not the live WebSocket connection itself.** A passing
  pre-flight check doesn't guarantee `live_ticker.py`'s stream won't
  drop mid-day - watch its terminal output during market hours too.
- **`position_sizing.py` only calculates a plan - it never places
  orders.** Risk-per-trade, ATR-based stop distance, and a hard max-
  position-% cap are all tunable constants at the top of the file.
- **`portfolio_risk.py` sizes trades against what's ALREADY open, not
  in isolation - THREE checks, not two.** (1) A total risk budget
  across all simultaneously open positions (default 6% of capital).
  (2) Same-symbol concentration - closes a real gap found during
  testing where the correlation check correctly excludes a symbol from
  being "correlated with itself," which meant nothing stopped
  accumulating multiple positions in the SAME stock; reproduced it
  concretely (combined exposure reached 37.9% of capital, blowing past
  the intended 20% cap) before fixing it. (3) A correlation-cluster cap
  (default 20%) across DIFFERENT positions that move together -
  measures REAL correlation from historical candle returns, not an
  assumed sector label, so it catches co-movement a sector
  classification would miss. All three verified against synthetic data
  with deliberately correlated/independent/concentrated scenarios.
  Uses a paper `open_positions` ledger (dashboard has Record/Close
  buttons, now also storing each position's stop-loss/take-profit) -
  not a real broker feed.
- **`position_monitor.py` shows live P&L (placed value vs current
  value) and a HOLD/SELL indicator per open position**, checked in
  order: stop-loss hit -> take-profit hit -> current rule-based signal
  reversed -> else HOLD. It only calculates a recommendation - closing
  a position is still a manual click. Every branch (long/short profit
  and loss, both exit triggers, signal reversal) was individually
  tested with known price/level combinations before trusting it.
- **`walk_forward_optimizer.py`'s fast path is vectorized (NumPy
  array ops instead of a per-candle Python loop) and was verified to
  produce byte-identical results to the original loop-based logic
  across 135 test cases before being trusted** - this is what makes
  500-symbol optimization take under a minute instead of several hours.
  Run `python walk_forward_optimizer.py --selftest [symbols]` any time
  to re-verify this on your own data. Parallel execution caps at
  `MAX_WORKERS = 4` rather than using every CPU core - spawning many
  processes that each reload scipy's compiled libraries at once can
  exceed Windows' default virtual memory (page file) size and crash
  with a DLL load error; lower `MAX_WORKERS` further (or raise it, if
  you've increased your page file) as needed.

## Known gaps / next steps

- Access token refresh is manual daily; a TOTP-based auto-login script
  would remove that step if it becomes tedious.
- `live_ticker.py` needs a static IP registered with Zerodha only if you
  later add order placement (not needed for data-only / paper trading).
- `portfolio_risk.py`'s correlation lookback (500 candles) and
  thresholds are fixed constants, not re-validated the way
  `walk_forward_optimizer.py` validates the decision thresholds. Worth
  the same rigor eventually.
- The ML model (`train_ml_model.py`) uses scikit-learn's
  `HistGradientBoostingClassifier`, not LightGBM/XGBoost - a deliberate
  substitution (both are histogram-based gradient boosting, same
  family) made because this project's dev environment couldn't install
  the other two; worth trying if you can install them in yours. No
  model versioning or drift detection yet either - each training run
  overwrites the previous model file.
- SEBI algo trading disclosure rules apply once this moves from personal
  signals to automated order placement - out of scope for this POC.
