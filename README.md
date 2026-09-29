# Intraday Trading Signal Agent

A local-first research pipeline for NSE intraday signals. It ingests live and
historical candles, computes deterministic technical/pattern features, and runs
**three independent decision paths side by side** — rule-based, LLM, and ML — so
their calls can be compared on real outcomes instead of argued about. On top of
that: ATR-based position sizing, portfolio-level risk caps, walk-forward
threshold validation, paper-position monitoring, and a FastAPI + React dashboard.
Underneath all of it, a live-feed watchdog that detects a stalled stream,
repairs the missing candles from the exchange's own record, and refuses to trade
on prices that no longer exist — see [Liveness and gap repair](#liveness-and-gap-repair).

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

**These numbers were measured on 5-minute candles — the old basis.** The
pipeline now stores 1-minute bars end to end (see
[Backfill](#backfill-one-time-or-an-occasional-top-up)), which changes what RSI,
ADX and ATR are actually measuring. Re-run `research.diagnose_edge` and
`research.backtest` on the new basis before reading anything below as current:
the measurement harness is sound, but every number in this table was produced
from a differently-sampled series.

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
│   ├── freshness.py                 Market-session + candle staleness — "is this data live?"
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
│   ├── fetch_historical.py          Resumable, rate-limited 1-min candle backfill
│   ├── gap_healer.py                Finds missing minutes, refetches them from Kite
│   ├── fetch_news.py                9 free feeds (Moneycontrol, ET, CNBC-TV18, Livemint,
│   │                                 Business Standard, NDTV Profit, BSE announcements),
│   │                                 symbol-tagged via word-boundary + company-name matching
│   └── live_ticker.py               KiteTicker WebSocket -> 1-min candles, heartbeat,
│                                    stall watchdog, heal-on-reconnect
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
│   └── routers/                     watchlist, positions, opportunities, digest, symbols,
│                                    wallet, liveness, history, pipeline
│
├── frontend/                       React + TypeScript + Vite dashboard
│   └── src/                         Hash-router pages (dashboard, transactions,
│                                    signal pipeline), polling hooks, Plotly chart, panels
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
python -m ingest.fetch_historical   # 60 days of 1-min candles, resumable if interrupted
```

The default interval is **minute**, deliberately matching what the live ticker
writes — a 5-minute history beside 1-minute live candles in one table made
"the last 200 candles" mean two different horizons and silently rescaled ATR
and RSI between backtest and session. Minute history is heavy (~375 rows per
symbol per day), so the default lookback is 60 days rather than a year:

```bash
python -m ingest.fetch_historical --days 120            # more minute history
python -m ingest.fetch_historical --interval 5minute    # the old behaviour
```

For 500 symbols, expect 30–60 minutes — Kite rate-limits historical requests.
Re-runs skip symbols already completed *for that interval* (tracked in the
`backfill_state` table, since the candles table alone cannot tell a minute bar
from a 5-minute one).

A backfill **replaces the window it covers**, so re-running with
`--interval minute` over an existing 5-minute history swaps it out instead of
leaving both bars behind for every fifth minute — which is the intended way to
migrate to the minute basis.

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
                                                         #    LLM (15 min) / feed watchdog (10 min) /
                                                         #    repair sweep (3:32 PM) / digest (3:35 PM)
python -m uvicorn api.main:app --reload --port 8000      # 5. dashboard API
cd frontend && npm run dev                               # 6. dashboard UI -> localhost:5173
```

Any time during the session, one command answers "is this live?":

```bash
python -m core.freshness   # feed status, staleness, heartbeat; also /api/liveness
```

`live` means the heartbeat is current and every candle is fresh. `degraded`
means the ticker is up but the data has fallen behind. `down` means no
heartbeat, or no socket, during market hours — restart the ticker, then run
`python -m ingest.gap_healer` to refill the hole.

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

At close, Ctrl+C the ticker first: it flushes its in-flight candle and then
runs an end-of-session repair sweep over every symbol, so anything lost during
the day comes back from the exchange's own record before you stop. The
scheduler does the same sweep at 3:32 PM if you would rather just leave it.
Ctrl+C the rest whenever — no cleanup needed.

---

## Command reference

| What | Command |
|---|---|
| Pre-flight check | `python -m scripts.validate_setup` |
| Refresh Kite token | `python -m ingest.kite_auth` |
| Build watchlist from NSE | `python -m ingest.build_nifty500_symbols` |
| Resolve instrument tokens | `python -m ingest.instrument_lookup symbols.txt` |
| Create/migrate the DB | `python -m storage.db` |
| Backfill history | `python -m ingest.fetch_historical [--days N] [--interval minute]` |
| Stream live candles | `python -m ingest.live_ticker` |
| Repair missing candles | `python -m ingest.gap_healer [--recent 90]` |
| Check feed liveness | `python -m core.freshness` |
| Feed health over HTTP | `curl localhost:8000/api/liveness` |
| Transaction history over HTTP | `curl localhost:8000/api/transactions` |
| Signal-path wiring over HTTP | `curl localhost:8000/api/pipeline` |
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

The liveness layer is tuned by constants in the same style:

| Constant | Where | Meaning |
|---|---|---|
| `FLUSH_INTERVAL_SEC` | `ingest/live_ticker.py` | how often buffered candles are written |
| `STALL_WARN_SEC` | `ingest/live_ticker.py` | silence before the feed is called stalled |
| `FORCE_RECONNECT_SEC` | `ingest/live_ticker.py` | silence before the socket is forced to re-handshake |
| `RECONNECT_MAX_TRIES` / `RECONNECT_MAX_DELAY_SEC` | `ingest/live_ticker.py` | how long an outage is ridden out before `on_noreconnect` alerts |
| `DEFAULT_MAX_AGE_MINUTES` | `core/freshness.py` | how far behind a candle may fall before it is refused |
| `HEARTBEAT_MAX_AGE_SEC` | `core/freshness.py` | heartbeat age that counts as "alive" |
| `HEARTBEAT_TRUST_SEC` / `FEED_DOWN_LOOKBACK_MIN` | `scripts/scheduler.py` | when the repair job stops trusting the ticker, and how far back it sweeps |
| `DEFAULT_INTERVAL` / `DEFAULT_DAYS_BACK` | `ingest/fetch_historical.py` | the stored candle basis and its default lookback |

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
- **A finished candle is handed off, never overwritten.** `on_ticks` appends the
  completed minute to a buffer the flush thread drains, instead of replacing the
  bucket dict. Replacing it lost the candle for any symbol busy enough to receive
  another tick before the next flush — which made data coverage *inversely*
  correlated with liquidity. See [docs/DESIGN.md](docs/DESIGN.md).
- **A candle timestamp has exactly one identity.** Everything that writes candles
  normalises to naive IST via `core/freshness.candle_timestamp()`, and reads
  compare 16-character minute prefixes, so the two forms that historically
  coexisted in `candles` (one tz-aware, one naive) can no longer create two rows
  for one minute.
- **Staleness is checked, not assumed.** No indicator, decision or gate reads a
  candle without `core/freshness.py` having a say in whether that candle is
  current. A dead feed and a quiet market produce the same absent rows, and only
  the clock can tell them apart.
- **The three decision paths are compared, never merged.** The rule, ML and LLM
  paths write to separate tables and the Pipeline page shows their calls side by
  side with their agreement, because the whole point of that separation is to
  earn trust with a track record before any of them is allowed to size a trade.

## Liveness and gap repair

A live feed that goes quiet is indistinguishable from a quiet market unless
something says so explicitly. Four things make it explicit:

1. **Heartbeat.** The ticker's flush thread upserts a row into `ingest_status`
   every 5 seconds (connected, last tick, buffer depth, repairs queued).
   `GET /api/liveness` turns it into one word — `live`, `degraded`, `down` or
   `closed`. `python -m core.freshness` prints the same thing.
2. **Stall watchdog.** Silence during market hours is a dead feed, not a quiet
   one: 500 symbols never all stop together. After 90s the ticker says so;
   after 300s it sends a close frame so the client re-handshakes and
   resubscribes. It deliberately does *not* call `close()`, which stops the
   retry loop and would turn a recoverable stall into a dead process.
3. **Repair.** Every detected outage window is refetched from Kite's own
   historical record by `ingest/gap_healer.py` — on reconnect, on a restart
   mid-session, every 10 minutes while the feed is down (via the scheduler),
   and definitively after the close. Each hole is recorded in `candle_gaps`,
   including the ones that recovered zero candles, because "the symbol
   genuinely didn't trade" and "we weren't listening" are different facts.
4. **Refusal.** `core/freshness.py` gates the decision cycles: a symbol whose
   candles are more than 10 minutes behind the live feed is skipped and named,
   rather than traded on a price that no longer exists. Outside the session
   nothing is stale, so backtests and after-hours runs are unaffected.

### Two bugs this uncovered

Both were found by looking at what the data actually contained, and both
produced exactly the symptom they look like — hours of information missing
while the process appeared to be running fine.

- **Finished candles were discarded on the tick path.** Replacing a bucket as
  soon as a tick from the next minute arrived dropped the completed candle
  unless the 5-second flush thread had already collected it. With ticks
  arriving in milliseconds, that lost the candle for every symbol busy enough
  to matter, so coverage was *inversely* correlated with liquidity: of one
  session's captured minutes, low-volume names had ~230 candles and HDFCBANK
  had 4. Completed minutes are now handed to a buffer the flusher drains.
- **Live and historical rows had different timestamp identities.** The
  backfill stored Kite's tz-aware `...+05:30` datetimes while the ticker wrote
  naive ones, so the same minute existed under two primary keys — unfixable by
  `INSERT OR REPLACE`, invisible to equality checks, and enough to make gap
  detection report every backfilled minute as missing. Writes now normalise
  through `core.freshness.candle_timestamp()`, and reads compare minute
  prefixes, so rows written before the fix are still read correctly.

## Dashboard

`frontend/` is a hash-routed React app: the header is the navigation bar, and
every page is a URL you can reload or bookmark (`#/dashboard`, `#/history`,
`#/pipeline`). Hash routing instead of the History API on purpose — the same
bundle then works from the Vite dev server, from any static host, and from
`file://`, with no server rewrite rules.

| Page | What it shows |
|---|---|
| `#/dashboard` | Everything that used to be one long scroll: Top Opportunity, digest, wallet, open positions, and the selected symbol's chart, patterns and signal history. The only page with the watchlist sidebar and the symbol picker. |
| `#/history` | Every money movement in the paper book (`wallet_transactions`) with the closed trade behind it (`trades`), plus win rate, slippage paid, best/worst trade and P&L by day. Read-only. |
| `#/pipeline` | The three decision paths side by side, per symbol: what the rules said, what the ML model said, what the LLM said, whether they agreed, and the rupee size the plan came out at. |

Top Opportunity marks any candidate you already hold with a `📌 Open` chip
carrying its live P&L; clicking the chip pops the position out with its stop,
target and exit recommendation, so "should I still be in this?" is answerable
without leaving the panel.

The Pipeline page is the honest picture of the wiring: the ML and LLM paths are
logged for comparison and never size or place anything, so position size comes
solely from the rule-based action. A row can therefore show the ML model
disagreeing while the plan is still sized — that is the connection, not a
contradiction. Agreement is measured over directional calls only: a HOLD is an
abstention, shown but never counted as a disagreement.

## Known gaps

- Access-token refresh is manual daily (a TOTP auto-login would remove it).
  The watchdog cannot recover from an expired token — it will retry every 30s
  and never succeed, which is why `on_noreconnect` shouts.
- There is no process supervisor: if the ticker process is killed, data stops
  until you restart it. The scheduler's repair job keeps filling the hole
  while that lasts, and the post-close sweep makes it whole, but a supervisor
  that restarts the process would be strictly better.
- Pre-existing databases keep their legacy `+05:30` candle rows. They read
  correctly, but they are only rewritten to the canonical form for minutes the
  healer repairs.
- `risk/portfolio_risk.py`'s correlation lookback and thresholds are fixed
  constants, not validated the way the decision thresholds are.
- No model versioning or drift detection — each training run overwrites
  `models/ml_model.joblib`.
- `walk_forward_optimizer.py` tunes **entry thresholds only**; it cannot
  validate exit logic, which is where the largest measurable gain is.
- The ticker's socket layer can't be tested offline (it opens a live WebSocket
  at import), but the aggregation logic now can: `on_ticks` and
  `_collect_buckets` are pure enough to drive directly with synthetic ticks.
  Still watch the terminal output during market hours.
- SEBI algo-trading disclosure rules apply once this moves from personal signals
  to automated order placement. Out of scope for this POC.

## Documentation

| Document | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Layering, data flow, dependency rules, DB schema, threading and API boundaries |
| [docs/DESIGN.md](docs/DESIGN.md) | Why the code is built this way — decisions, the bugs behind them, trade-offs (the liveness section covers the candle-loss and timestamp-identity bugs) |
| [docs/EDGE_ANALYSIS.md](docs/EDGE_ANALYSIS.md) | Measured evidence that the signal has no edge, and what to change |
| [docs/STRATEGIES.md](docs/STRATEGIES.md) | The candidate strategies, how they're measured, and the promotion rules |
