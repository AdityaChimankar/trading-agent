# Intraday Trading Signal Agent

A local-first AI agent that combines real-time market data, deterministic
technical/pattern analysis, and LLM-based sentiment scoring to generate
intraday BUY/SELL/HOLD signals for NSE stocks, with ATR-based position
sizing and walk-forward parameter validation. Paper-trading signals only -
no live order execution.

## Project structure

```
trading-agent/
├── README.md                        This file
├── requirements.txt                  All Python dependencies
├── .env.example                       Credential template (Kite + Gemini keys)
├── schema.sql                         SQLite schema
├── db.py                              DB connection (WAL mode) + watchlist single source of truth
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
├── digest.py                           End-of-day narrative summary
├── rankings.py                         Watchlist-wide bullish/bearish scoring (dashboard sidebar)
├── position_sizing.py                  ATR-based position sizing + stop-loss/take-profit
├── portfolio_risk.py                    Portfolio-level risk: correlation clusters + total risk budget
│
├── backtest.py                         Vectorized backtesting harness (single/multi-symbol/--all)
├── walk_forward_optimizer.py          Rolling train/test parameter re-fitting, out-of-sample validated
├── scheduler.py                        Automation: news -> sentiment -> decisions -> LLM compare -> digest
├── dashboard.py                        Streamlit dashboard: multi-panel chart, sidebar rankings, position sizing
│
└── data/
    └── trading_agent.db                SQLite database (created by db.py)
```

## Tech stack & cost

| Layer | Choice | Cost |
|---|---|---|
| Market data + news | Zerodha Kite Connect | ~Rs.500/month |
| Watchlist sourcing | NSE Nifty 500 CSV + `requests` | Rs.0 |
| Quant + patterns | pandas, pandas-ta-style indicators, scipy | Rs.0 |
| Sentiment + LLM decisions | Gemini Flash-Lite API | ~Rs.400-1200/month |
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
```

`backtest.py` checks win rate, risk-reward ratio, and max drawdown with
fixed default thresholds. `walk_forward_optimizer.py` goes further - it
re-fits RSI/ADX thresholds per stock on rolling training windows and
validates on genuinely out-of-sample data it never saw during tuning,
telling you whether per-stock optimization actually helps or whether the
fixed defaults already fit fine. Both run across your whole watchlist in
well under a minute even at 500 symbols (vectorized). Don't skip this -
it's the only thing that tells you whether the rule-based logic actually
has an edge before it runs live.

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
python scheduler.py          # 4. runs news / sentiment / decisions (5 min) / LLM compare (15 min) / digest (3:35 PM)
streamlit run dashboard.py   # 5. optional - open http://localhost:8501 to watch live
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
  values - normal live runs are unaffected. `llm_decision_agent.py` runs
  alongside for comparison, writing to a *separate* `llm_signals` table -
  it never feeds into the rule-based decision and isn't a replacement
  until its agreement rate + real outcomes justify that.
- **SQLite runs in WAL mode with a 30s busy_timeout** (set in
  `db.py`'s `get_connection()`), since `live_ticker.py`, `scheduler.py`,
  and `dashboard.py` all access the DB concurrently. On top of that,
  every write loop (decision cycles, LLM comparison cycles) commits
  **per-symbol**, not once at the end of a whole watchlist scan -
  holding one write transaction open across hundreds of symbols
  (especially with real network-latency LLM calls in between) can
  exceed even a generous busy_timeout and cause "database is locked"
  errors in other processes trying to write at the same time.
- **`live_ticker.py`'s tick callback (`on_ticks`) does ONLY in-memory
  dict updates - no DB access at all.** All SQLite writes happen in a
  separate background thread that flushes completed minute-buckets
  every few seconds. Kite's own guidance is that the WebSocket
  connection gets dropped if `on_ticks` blocks on calculation or I/O -
  doing DB writes inside that callback at scale (hundreds of symbols)
  was the root cause of repeated 1006 disconnects.
- **Kite Connect has no news API.** News comes from free RSS feeds
  (Moneycontrol, Economic Times), keyword-matched to your watchlist.
- **Backtest and walk-forward sentiment is neutral (0.0).** Historical
  news isn't backfilled candle-by-candle the way price is, so both
  currently validate the RSI/ADX/pattern logic in isolation. Once
  you've run the live pipeline for a few weeks, real historical
  sentiment will exist for future backtests.
- **No transaction costs/slippage modeled** in the backtest or
  walk-forward optimizer - real fills will be slightly worse than the
  raw numbers shown.
- **`validate_setup.py` checks the REST APIs (Kite quotes, news feeds,
  Gemini), not the live WebSocket connection itself.** A passing
  pre-flight check doesn't guarantee `live_ticker.py`'s stream won't
  drop mid-day - watch its terminal output during market hours too.
- **`position_sizing.py` only calculates a plan - it never places
  orders.** Risk-per-trade, ATR-based stop distance, and a hard max-
  position-% cap are all tunable constants at the top of the file.
- **`portfolio_risk.py` sizes trades against what's ALREADY open, not
  in isolation.** Two checks: a total risk budget across all
  simultaneously open positions (default 6% of capital), and a
  correlation-cluster cap (default 20%) that catches concentrated risk
  even across positions in nominally different stocks - it measures
  REAL correlation from historical candle returns, not an assumed
  sector label, so it catches co-movement a sector classification
  would miss. Verified against synthetic data: a deliberately
  correlated pair (0.99 measured correlation) correctly triggers the
  cluster cap, while an independent stock (0.07 correlation) is
  correctly left unaffected by it. Uses a paper `open_positions`
  ledger (dashboard has Record/Close buttons) - not a real broker
  feed.
- **`walk_forward_optimizer.py`'s fast path is vectorized (NumPy
  array ops instead of a per-candle Python loop) and was verified to
  produce byte-identical results to the original loop-based logic
  across 135 test cases before being trusted** - this is what makes
  500-symbol optimization take under a minute instead of several hours.

## Known gaps / next steps

- Access token refresh is manual daily; a TOTP-based auto-login script
  would remove that step if it becomes tedious.
- `live_ticker.py` needs a static IP registered with Zerodha only if you
  later add order placement (not needed for data-only / paper trading).
- Position sizing is per-trade AND now portfolio-aware (correlation
  clusters + total risk budget via `portfolio_risk.py`) - but the
  correlation lookback (500 candles) and thresholds are fixed
  constants, not re-validated the way `walk_forward_optimizer.py`
  validates the decision thresholds. Worth the same rigor eventually.
- No ML layer yet - all decisions are rule-based (RSI/ADX/pattern
  thresholds), not a trained model. Worth considering only after the
  rule-based backtest gives a real baseline to try to beat.
- SEBI algo trading disclosure rules apply once this moves from personal
  signals to automated order placement - out of scope for this POC.
