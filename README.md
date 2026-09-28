# Intraday Trading Signal Agent

A local-first research pipeline for NSE intraday signals. It ingests live and
historical candles, computes deterministic technical/pattern features, and runs
**three independent decision paths side by side** — rule-based, LLM, and ML — so
their calls can be compared on real outcomes instead of argued about. On top of
that: ATR-based position sizing, portfolio-level risk caps, walk-forward
threshold validation, paper-position monitoring, and a FastAPI + React dashboard.

**Paper trading only.** Nothing in this repository places a real order. The
`open_positions` table is a manual ledger you fill in by clicking a button.

---

## ⚠️ Current status: the rule-based signal has no measured edge

Read this before you run anything live. `research/diagnose_edge.py` measures the
signal against the only benchmark that matters — **entering at a random candle** —
and the entry rule does not beat it:

| | gross EV / trade | vs random | t |
|---|---|---|---|
| random entry (baseline long) | +0.0014% | — | +3.3 |
| **all signals** (direction-adjusted) | **−0.0042%** | −0.0042% | −4.5 |
| BUY signals (n = 117,349) | −0.0006% | −0.0020% | −0.4 |
| SELL signals (n = 389,039) | −0.0053% | −0.0040% | −4.9 |

BUY is statistically indistinguishable from a coin flip (t = −0.4). The "pattern
confirmation" gate is not a gate at all — a bullish or bearish pattern code is
present on **98.0%** of candles, so it filters almost nothing while structurally
biasing the book ~76% short. Slippage is 0.05% per trade; the best edge found
anywhere in the ablation is 0.009% — about 5× too small to survive costs.

The single largest measured improvement is an exit-logic change: **removing the
fixed take-profit** gains ~+0.009%/trade (~33% relative) versus the exit logic
currently traded live, because the 2:1 target fires on 23.4% of trades while the
1.5×ATR stop fires on 55.9%.

Full numbers, method, tolerance/horizon/component sweeps and ranked
recommendations: **[docs/EDGE_ANALYSIS.md](docs/EDGE_ANALYSIS.md)**.

Treat this repo as a *measurement harness* — that part works well. The strategy
is a hypothesis that has not held up, and no amount of RSI/ADX threshold tuning
will fix it (that would be curve-fitting noise). See the docs for what to change.

---

## Project structure

Layers point downward only: `storage` → `core` → `{analysis, strategy, risk}` →
`research`. `scripts/` orchestrates, `api/` reads.

```
trading-agent/
├── paths.py                        Every file location in the project, in one place
├── symbols.txt                     Your watchlist seed (one NSE symbol per line)
├── requirements.txt
├── .env.example                    Credential template (Kite + Gemini keys)
│
├── storage/                        SQLite persistence
│   ├── db.py                        Connection (WAL), schema init, migrations, watchlist reads
│   └── schema.sql                   Table definitions
│
├── core/                           Deterministic primitives — no LLM, no I/O side effects
│   ├── quant_indicators.py          RSI, ATR, ADX
│   ├── pattern_detection.py         Candlestick + swing patterns
│   └── position_sizing.py           ATR-based size, stop-loss, take-profit
│
├── ingest/                         Getting market data in
│   ├── kite_auth.py                 Daily Zerodha login -> .access_token
│   ├── build_nifty500_symbols.py    NSE Nifty 500 list -> symbols.txt
│   ├── instrument_lookup.py         symbols.txt -> instrument tokens
│   ├── instruments.csv              Kite instrument dump (~9 MB, downloaded, tracked)
│   ├── watchlist_resolved.py        Generated token map (read by fetch_historical)
│   ├── fetch_historical.py          Resumable, rate-limited 5-min candle backfill
│   ├── fetch_news.py                Free RSS feeds, keyword-tagged to symbols
│   └── live_ticker.py               KiteTicker WebSocket -> 1-min candles
│
├── analysis/                       Read-only intelligence over stored data
│   ├── sentiment.py                 Gemini sentiment scoring on tagged news
│   ├── rankings.py                  Watchlist-wide bullish/bearish scoring
│   ├── opportunity_finder.py        Conviction-ranked top picks
│   ├── technical_summary.py         Human-readable indicator read
│   └── digest.py                    End-of-day narrative summary
│
├── strategy/                       The three decision paths + experimental candidates
│   ├── decision_agent.py            Rule-based — THE tested path
│   ├── llm_decision_agent.py        Gemini decisions, parallel comparison
│   ├── ml_decision_agent.py         Trained-model decisions, parallel comparison
│   ├── ml_features.py               Feature engineering (no lookahead, verified)
│   ├── train_ml_model.py            Chronological train/test split + trade simulation
│   ├── candidates.py                Candidate entries (long-only, evidence-derived)
│   └── shadow.py                    Logs candidate votes to strategy_signals, live
│
├── risk/
│   ├── portfolio_risk.py            Risk budget + same-symbol + correlation caps
│   └── position_monitor.py          Live P&L and HOLD/SELL per open position
│
├── research/                       Offline validation
│   ├── backtest.py                  Vectorized harness, --selftest
│   ├── walk_forward_optimizer.py    Rolling out-of-sample threshold fitting
│   ├── diagnose_edge.py             Is there any edge at all? (baseline + ablations)
│   └── strategy_lab.py              Measures candidates vs baselines, with costs
│
├── scripts/
│   ├── scheduler.py                 The daily automation loop
│   └── validate_setup.py            Pre-flight check before market hours
│
├── api/                            FastAPI JSON layer — NO trading logic
│   ├── main.py                      App, CORS, startup DB init, router registration
│   ├── serializers.py               numpy/pandas/dataclass -> JSON-safe conversion
│   └── routers/                     watchlist, positions, opportunities, digest, symbols
│
├── frontend/                       React + TypeScript + Vite dashboard
│   └── src/                         App shell, polling hooks, Plotly chart, panels
│
├── models/ml_model.joblib          Trained ML model artifact
└── data/trading_agent.db           SQLite database (gitignored, created by storage/db.py)
```

Details in **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

---

## Quickstart

**Every command below is run from the repository root** — that is a requirement
of the `python -m` form, and it is what makes the package imports resolve.

```bash
pip install -r requirements.txt
```

1. Sign up at https://developers.kite.trade/signup and subscribe to the paid
   Kite Connect plan (required for live *and* historical data).
2. Get a free Gemini API key at https://ai.google.dev.
3. Copy `.env.example` to `.env` and fill in `KITE_API_KEY`, `KITE_API_SECRET`,
   and `GEMINI_API_KEY`.
4. Build the watchlist and create the database:

```bash
python -m ingest.build_nifty500_symbols        # -> symbols.txt (or write your own list)
python -m ingest.instrument_lookup symbols.txt # -> ingest/watchlist_resolved.py
python -m storage.db                           # create/migrate the schema
```

5. Install the front end's dependencies (Node 18+):

```bash
cd frontend && npm install && cd ..
```

## Backfill (one-time, or an occasional top-up)

```bash
python -m ingest.kite_auth          # daily login: opens a URL, paste back the request_token
python -m ingest.fetch_historical   # ~1 year of 5-min candles, resumable if interrupted
```

For 500 symbols, expect 30–60 minutes — Kite rate-limits historical requests.
Re-runs skip symbols that are already backfilled.

## Validate before trusting anything

```bash
python -m research.backtest --all
python -m research.diagnose_edge --n 120        # is there an edge at all?
python -m research.walk_forward_optimizer --all
python -m research.backtest --selftest          # verify the fast path on YOUR data
python -m strategy.train_ml_model --all         # optional: the ML comparison model
```

`research/backtest.py` reports win rate, risk-reward and max drawdown at the
default thresholds. `research/walk_forward_optimizer.py` re-fits RSI/ADX per
stock on rolling windows and validates out-of-sample — the only number here that
isn't in-sample. `research/diagnose_edge.py` is the one to run first: it tells
you whether there is anything to tune, which the other two cannot answer.

## Daily routine (trading days, 9:15 AM – 3:30 PM IST)

```bash
python -m ingest.kite_auth        # 1. refresh today's access token (required daily, ~1 min)
python -m scripts.validate_setup  # 2. pre-flight: Kite, news feeds, Gemini, DB all working
```

`scripts/validate_setup.py` exits 0 on success and 1 on failure, so you can
chain it: `python -m scripts.validate_setup && python -m ingest.live_ticker`
refuses to start on a failed check.

Then, in separate terminals:

```bash
python -m ingest.live_ticker                             # 3. live candle stream
python -m scripts.scheduler                              # 4. news / sentiment /
                                                         #    decisions (5 min) / ML (5 min) /
                                                         #    LLM (15 min) / digest (3:35 PM)
python -m uvicorn api.main:app --reload --port 8000      # 5. dashboard API
cd frontend && npm run dev                               # 6. dashboard UI -> localhost:5173
```

The UI polls on its own schedule (positions and charts every 15 s, rankings and
opportunities every 60 s) and the top bar shows a `⟳ live` indicator while a
request is in flight. FastAPI serves interactive endpoint docs at
http://localhost:8000/docs.

The scheduler also runs a **shadow cycle** every 5 minutes: the candidate
strategies in `strategy/candidates.py` vote on the latest candle and their
votes are logged to the `strategy_signals` table — alongside the rule agent's
action on the same candle — without influencing any live decision. That table
is the out-of-sample track record used by the promotion rules in
[docs/STRATEGIES.md](docs/STRATEGIES.md).

At close, Ctrl+C all four — no cleanup needed.

---

## Command reference

| What | Command |
|---|---|
| Pre-flight check | `python -m scripts.validate_setup` |
| Refresh Kite token | `python -m ingest.kite_auth` |
| Build watchlist from NSE | `python -m ingest.build_nifty500_symbols` |
| Resolve instrument tokens | `python -m ingest.instrument_lookup symbols.txt` |
| Create/migrate the DB | `python -m storage.db` |
| Backfill history | `python -m ingest.fetch_historical` |
| Stream live candles | `python -m ingest.live_ticker` |
| Run the daily automation | `python -m scripts.scheduler` |
| Backtest | `python -m research.backtest [SYMBOLS... \| --all \| --selftest]` |
| Walk-forward validation | `python -m research.walk_forward_optimizer [SYMBOLS... \| --all \| --selftest]` |
| Edge diagnostics | `python -m research.diagnose_edge [--n N \| --all \| SYMBOLS...] [--exits]` |
| Strategy lab | `python -m research.strategy_lab [--n N \| --all] [--stop-mult 3.0]` |
| Shadow log candidates | automatic via scheduler, or `python -m strategy.shadow` |
| Train the ML model | `python -m strategy.train_ml_model [SYMBOLS... \| --all]` |
| ML decision cycle | `python -m strategy.ml_decision_agent` |
| Digest (end of day) | `python -m analysis.digest` |
| Dashboard API | `python -m uvicorn api.main:app --reload --port 8000` |
| Dashboard UI | `cd frontend && npm run dev` |
| Frontend typecheck/build | `cd frontend && npx tsc --noEmit && npm run build` |

## Configuration

Credentials live in `.env`:

| Variable | Purpose |
|---|---|
| `KITE_API_KEY` | Kite Connect API key |
| `KITE_API_SECRET` | Kite Connect API secret |
| `GEMINI_API_KEY` | Gemini API key (sentiment + LLM decisions) |

`.access_token` (gitignored, written by `ingest/kite_auth.py`) holds the daily
Kite session. Everything else is a named constant at the top of the relevant
module — the ones that change behaviour most are `SLIPPAGE` and `DEFAULT_PARAMS`
in `research/backtest.py`, and `RISK_PER_TRADE_PCT`, `ATR_STOP_MULTIPLIER`,
`REWARD_RISK_RATIO`, `MAX_POSITION_PCT_OF_CAPITAL` in `core/position_sizing.py`.

---

## Key design decisions

A short version — the reasoning, the bugs that forced each choice, and the
trade-offs are in **[docs/DESIGN.md](docs/DESIGN.md)**.

- **The `watchlist` DB table is the single source of truth.** No module hardcodes
  a symbol list. A hardcoded list once silently ran signals on 2 of 500 symbols.
- **`strategy/decision_agent.py` is the tested path.** `llm_decision_agent.py`
  and `ml_decision_agent.py` run alongside it and write to their own
  `llm_signals` / `ml_signals` tables. Neither feeds the rule-based decision.
- **SQLite runs in WAL mode, and every write loop commits per symbol.** Holding
  one write transaction across 500 symbols while making network LLM calls in
  between is what produced "database is locked".
- **`ingest/live_ticker.py` never touches the DB from its tick callback.** All
  writes happen on a background flush thread; blocking `on_ticks` caused
  repeated WebSocket 1006 disconnects at scale.
- **The API holds no trading logic.** Every endpoint calls the same Python
  modules the CLI does, so each calculation has exactly one implementation.

## Known gaps

- Access-token refresh is manual daily (a TOTP auto-login would remove it).
- `risk/portfolio_risk.py`'s correlation lookback and thresholds are fixed
  constants, not validated the way the decision thresholds are.
- No model versioning or drift detection — each training run overwrites
  `models/ml_model.joblib`.
- `walk_forward_optimizer.py` tunes **entry thresholds only**; it cannot
  validate exit logic, which is where the largest measurable gain is.
- `ingest/live_ticker.py` is the one module that can't be tested offline (it
  opens a live WebSocket at import) — watch its terminal output during market
  hours.
- SEBI algo-trading disclosure rules apply once this moves from personal signals
  to automated order placement. Out of scope for this POC.

## Documentation

| Document | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Layering, data flow, dependency rules, DB schema, threading and API boundaries |
| [docs/DESIGN.md](docs/DESIGN.md) | Why the code is built this way — decisions, the bugs behind them, trade-offs |
| [docs/EDGE_ANALYSIS.md](docs/EDGE_ANALYSIS.md) | Measured evidence that the signal has no edge, and what to change |
| [docs/STRATEGIES.md](docs/STRATEGIES.md) | The candidate strategies, how they're measured, and the promotion rules |
