# Architecture

## Layering

Packages form a strict stack. Arrows point in the direction of the import — a
layer may import the layers below it, never above.

```
                       scripts/            (orchestration: scheduler, pre-flight)
                          │
        ┌─────────────────┼──────────────────────────┐
        ▼                 ▼                          ▼
   strategy/          analysis/                   ingest/         (all use core + storage)
        │                 │                          │
        └────────┬────────┘                          │
                 ▼                                   │
              risk/  ─────────────────────────────────┤
                 │                                   │
                 ▼                                   ▼
              core/  ◄────────────────────────────────┘      (indicators, patterns, sizing)
                 │
                 ▼
             storage/                                   (SQLite: connection, schema, watchlist)
                 │
                 ▼
              paths.py                                  (file locations; no imports at all)

   research/  sits beside the stack: it imports core, storage and strategy,
              and nothing imports it except diagnostics tooling and strategy's
              shared-helper import (see "Known inversion" below).

   api/  is a read-only façade — it imports any layer, nothing imports it.
   frontend/  only ever talks to api/ over HTTP.
```

### What each package is allowed to do

| Package | Responsibility | Not allowed |
|---|---|---|
| `paths.py` | Resolve every file location from `__file__` | Import anything project-local |
| `storage/` | SQLite connection, schema, migrations, watchlist reads | Contain trading logic |
| `core/` | Deterministic math: indicators, patterns, sizing | Call an LLM, write files, place orders |
| `ingest/` | Talk to Kite/NSE/RSS, write candles and news | Decide anything |
| `analysis/` | Read stored data and describe it | Write signals |
| `strategy/` | Turn features into BUY/SELL/HOLD | Size positions or enforce portfolio limits |
| `risk/` | Allocate capital, cap exposure, monitor positions | Generate entry signals |
| `research/` | Offline validation of `strategy/` | Run during market hours |
| `scripts/` | Wire the pipeline into a schedule | Contain business logic |
| `api/` | Serialize existing functions to JSON | Re-implement any calculation |

## Known inversion: `strategy/` → `research/`

Three strategy modules import shared helpers from `research/backtest.py`:

- `strategy/ml_features.py` → `prepare_symbol_series`, `precompute_pattern_bias_codes`
- `strategy/ml_decision_agent.py` → the same two
- `strategy/candidates.py`, `strategy/session_features.py` → `prepare_symbol_series`
  constants (`SWING_ORDER`, `DEFAULT_PARAMS`, `FORWARD_WINDOW`, `SLIPPAGE`)

That is backwards: validation code should depend on the strategy, not the other
way around. It is a deliberate, documented wart rather than a bug — these helpers
are the *canonical* implementations, and `research/backtest.py` is where they were
written and verified. `research/backtest.py` itself imports
`strategy/decision_agent.py`'s `evaluate_signals`, so the two packages reference
each other.

There is **no import-time cycle** (importing `strategy.ml_features` pulls in
`research.backtest`, which pulls in `strategy.decision_agent`, which imports only
`core`/`storage` — the chain terminates). But the layering is wrong, and the fix
is to lift the shared pieces — `SLIPPAGE`, `DEFAULT_PARAMS`,
`precompute_pattern_bias_codes`, `vectorized_evaluate`, `prepare_symbol_series`
— into a new `core/signals.py`, leaving `research/backtest.py` as a caller. That
makes `strategy → core` and `research → {core, strategy}`, with no inverted edge.

Note the constraint that fix has to respect: `strategy/session_features.py` must
keep importing `SLIPPAGE` and `FORWARD_WINDOW` from `research/backtest.py` (not
copy them), because a second copy of a cost constant is exactly how a silent
behavioural drift starts — see the same reasoning for `DEFAULT_PARAMS` in
`research/backtest.py`.

## Candidate strategies: pure, memoized, and self-verifying

`strategy/session_features.py` builds every session/regime/cost array
(session VWAP, ATR percentile, HTF momentum, opening gap, expected move) once
per symbol and **memoizes them on the `prepared` dict**, because
`research/strategy_lab.py` calls each candidate function once per candle per
symbol. Every array it produces is causal — a trailing window or a within-day
cumulative sum, never a centered window or a full-series normalization — and
`python -m strategy.session_features` proves it by rebuilding every feature over
a truncated prefix and requiring identical values. `python -m strategy.candidates`
separately asserts each candidate's admit-rate, because a mis-scaled threshold
admits either nothing or almost everything, and both look like a result in the
lab's table. See [STRATEGIES.md](STRATEGIES.md) for why both checks exist and
what they have already caught.


## Data flow, end to end

```
                      ┌──────────────────────────────────────────────┐
  Kite WebSocket ───► │ ingest/live_ticker.py                        │
  (1-min buckets)     │  ticker thread: in-memory only               │
                      │  flush thread:  → candles                     │
                      └──────────────────────────────────────────────┘
  Kite REST ────────► ingest/fetch_historical.py ──► candles
  RSS feeds ────────► ingest/fetch_news.py ────────► news
                                        │
                                        ▼
                            analysis/sentiment.py ──► sentiment
                                        │
   candles ─────► core/quant_indicators.py   (RSI, ATR, ADX)
                  core/pattern_detection.py  (candles + swings)
                                        │
                                        ▼
                          strategy/decision_agent.py  ──► signals
                          strategy/llm_decision_agent.py ──► llm_signals
                          strategy/ml_decision_agent.py  ──► ml_signals
                                        │
                                        ▼
                            analysis/digest.py ──► digests

   On demand / by hand:
     analysis/rankings.py ──► analysis/opportunity_finder.py   (conviction picks)
     core/position_sizing.py ──► risk/portfolio_risk.py ──► open_positions
     risk/position_monitor.py  ──► live P&L + HOLD/SELL per position
```

The three decision agents are **parallel, not sequential**. `decision_agent.py`
is the only one that counts; the LLM and ML paths exist to build a comparison
track record on identical inputs. They write to separate tables precisely so a
comparison can never contaminate the tested path.

## Storage schema

SQLite, WAL mode, `data/trading_agent.db`. Defined in `storage/schema.sql` and
created/migrated by `storage/db.py`:

| Table | Written by | Contents |
|---|---|---|
| `watchlist` | `ingest/fetch_historical.py` | symbol, instrument_token, exchange — **the single source of truth** |
| `candles` | `ingest/fetch_historical.py`, `ingest/live_ticker.py`, `ingest/gap_healer.py` | OHLCV bars per symbol/timestamp, 1-minute basis |
| `backfill_state` | `ingest/fetch_historical.py` | which `(symbol, interval)` ranges are complete — the candles table cannot answer that once more than one interval has ever been stored |
| `ingest_status` | `ingest/live_ticker.py` | heartbeat singleton: connected, last tick, buffer depth, repairs queued |
| `candle_gaps` | `ingest/gap_healer.py` | every hole detected, and whether it was repaired (including repairs that recovered 0 candles) |
| `news` | `ingest/fetch_news.py` | RSS items keyword-tagged to symbols |
| `sentiment` | `analysis/sentiment.py` | OpenRouter score per news item |
| `signals` | `strategy/decision_agent.py` | Rule-based BUY/SELL/HOLD (the tested path) |
| `llm_signals` | `strategy/llm_decision_agent.py` | LLM calls, for comparison only |
| `ml_signals` | `strategy/ml_decision_agent.py` | Model calls, for comparison only |
| `strategy_signals` | `strategy/shadow.py` | Candidate votes, logged beside the rule agent's action on the same candle |
| `digests` | `analysis/digest.py` | End-of-day narrative |
| `open_positions` | `risk/portfolio_risk.py` (via the API) | Paper ledger, incl. stop_loss / take_profit |
| `trades` | `risk/wallet.py` (via the API) | Closed trades: entry/exit, realized P&L, exit reason |
| `wallet_settings` | the API | Singleton: paper capital the book builds from |
| `wallet_transactions` | the API | Deposits, withdrawals, and realized P&L, in order |
| `wallet_daily_snapshots` | the API | Per-day capital / P&L snapshot |

Timestamps in `candles` are naive IST at minute precision. One consequence
worth knowing before writing a query: pre-existing rows may carry Kite's
`+05:30` suffix from the original backfill, so compare the 16-character minute
prefix (`substr(timestamp, 1, 16)`) rather than the raw string, or use
`core/freshness.minute_key()`. Both forms sort correctly as strings.

`init_db()` is idempotent and self-healing — it runs `schema.sql` and then adds
any column missing from an existing table. `api/main.py` calls it on startup,
which is what closes the "I forgot to re-run `python -m storage.db` after
upgrading" gap.

## Concurrency model

Four processes touch the DB during market hours, so contention is the default
assumption rather than an edge case:

- **WAL mode + 30 s `busy_timeout`** (`storage/db.py`) lets readers and one
  writer coexist instead of failing fast with "database is locked".
- **`ingest/live_ticker.py` commits nothing from the tick callback.** `on_ticks`
  does in-memory dict updates only; a separate thread drains completed
  minute-buckets every 5 s. Blocking `on_ticks` on I/O is what caused repeated
  WebSocket 1006 disconnects at scale.
- **A second background thread does network repair.** `heal_loop` consumes
  outage windows from a queue and refetches them from Kite's historical
  endpoint, throttled to ~2.5 req/s. It is separate from the flush thread so a
  rate-limited sweep can never delay a write, and the flush thread's loop is
  wrapped so it cannot die silently with a day's candles still in memory.
- **Repair is idempotent and cheap when nothing is wrong.** `gap_healer`
  compares the session timeline against stored minutes and fetches only the
  holes, so a healthy sweep costs one indexed query per symbol and no requests
  at all — which is what makes it safe to run on reconnect, on a restart, every
  10 minutes while the feed is down, and after the close.
- **Every write loop commits per symbol**, not once per watchlist scan. Holding a
  transaction open across 500 symbols — with network-latency LLM calls in the
  middle — can outlast even a generous `busy_timeout`.
- **`MAX_WORKERS = 4`** in the parallel research tools, rather than
  `os.cpu_count()`. Many processes each re-importing scipy's compiled libraries
  simultaneously can exhaust Windows' default page file and die with a DLL load
  error.

## API and frontend boundary

`api/` is a pure JSON façade. It contains **no trading logic** — each endpoint
imports and calls the same function the CLI calls, so there is exactly one
implementation of every calculation.

| Endpoint | Delegates to |
|---|---|
| `GET /api/health` | — |
| `GET /api/watchlist` | `storage.db`, `analysis.rankings`, `risk.position_monitor` |
| `GET /api/rankings` | `analysis.rankings` |
| `GET /api/positions` | `risk.position_monitor` |
| `POST /api/positions` | `risk.portfolio_risk.calculate_portfolio_adjusted_position` |
| `POST /api/positions/{id}/close` | `risk.portfolio_risk.close_position` |
| `GET /api/opportunities` | `analysis.opportunity_finder` |
| `GET /api/digest` | `analysis.digest` |
| `GET /api/symbols/{symbol}/{chart,patterns,sizing,news,signals,llm-signals,ml-signals}` | `storage.db`, `core.quant_indicators`, `core.pattern_detection`, `risk.portfolio_risk` |
| `GET /api/liveness`, `GET /api/liveness/gaps` | `core.freshness`, `ingest.gap_healer` |
| `GET /api/wallet` | `risk.wallet`, `risk.position_monitor` |
| `GET /api/transactions` | `storage.db` (`wallet_transactions`, `trades`) |
| `GET /api/pipeline` | `analysis.rankings`, `storage.db` (`signals`, `ml_signals`, `llm_signals`) |

Only the position and wallet endpoints are mutations, and all of them write to
the paper ledger — nothing reaches a broker.

Two details worth knowing:

- **`api/serializers.py` exists because NaN is not valid JSON.** The analysis
  modules return dataclasses, `sqlite3.Row`s and numpy scalars; everything
  crossing the boundary is normalized there, so a NaN indicator arrives as
  `null` instead of breaking the response.
- **Transport is separate from the layers.** In dev, Vite (`:5173`) proxies
  `/api` to uvicorn on `:8000` and CORS allows localhost origins. The front end
  never imports Python and never computes a signal — it renders what the API
  returns. That is why `npm run build` and `npx tsc --noEmit` are the only
  frontend gates that matter.
- **Routing is hash-based and dependency-free.** `frontend/src/lib/router.ts` is
  ~30 lines instead of a router inside the app, and three views do not justify a
  dependency. Every unknown or missing hash resolves to the dashboard, so a bad
  URL degrades to a working page rather than a blank screen.

## Verifying a change

There is no test suite; verification is by self-checking entry points and
cheap smoke tests. In rough order of value:

```bash
python -m py_compile paths.py storage/*.py core/*.py ingest/*.py analysis/*.py \
    strategy/*.py risk/*.py research/*.py scripts/*.py api/*.py api/routers/*.py

python -m research.backtest --selftest             # vectorized fast path == slow reference
python -m research.walk_forward_optimizer --selftest 360ONE   # 27/27 grid combos match
python -m strategy.session_features                 # candidate features cannot see the future
python -m strategy.candidates                       # candidate admit-rates are plausible
python -m strategy.ml_features 360ONE              # feature build produces labelled rows
cd frontend && npx tsc --noEmit                    # strict TS
```

`research/backtest.py --selftest` and
`research/walk_forward_optimizer.py --selftest` are the load-bearing ones: they
independently confirm that the vectorized fast paths reproduce the slow
per-candle loops on *your* stored data, which is the assumption the entire
validation story rests on.

The two `strategy/` self-tests guard a different assumption. A feature that
peeks at the future, or a gate whose thresholds are in the wrong units, does not
raise — it prints a perfectly plausible number in the lab table. Prefix
invariance and admit-rate assertions are what stop that from being read as a
result.

Because every command is a `python -m` invocation, **the working directory must
be the repository root**. That is also what guarantees `paths.py` and `.env` are
found.
