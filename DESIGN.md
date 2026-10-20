# System design

Last reviewed: 2026-10-20

## Outcome-evidence boundary

The BUY/SELL lifecycle adds `outcome_contract.py` (features, policy, calibration/arbitration and gates), `train_outcome_models.py` (four chronological windows and durable holdout reservations), `outcome_paper.py` (prospective intent ledger, settlement and guarded promotion), and `outcome_model.py` (dashboard adapter). Only `outcome_cli promote` selects a candidate; training never replaces `ml_model.joblib`. Model/paper/feature source digests and artifact checksums prevent silently loading incompatible contracts. Locally trained joblib files are trusted objects, not safe imports from arbitrary sources.

`/research/outcome-models` is read-only lifecycle status. The full scanner loads the promoted adapter when an active pointer exists, otherwise the legacy classifier. Invalid active evidence fails closed. Qualified outcome estimates apply only to the evaluated ML-only policy, trained symbols and fixed bracket configuration; hybrid estimates do not inherit qualification. Shadow evidence is separate from the operational paper ledger and does not simulate shared capital or real market fills.

`research/trade_outcomes.py` owns conservative next-open stop/target replay. `analysis/outcome_validation.py` owns fixed-rule chronological calibration/holdout reports, sample/expectancy/Brier gates, policy matching and expiry. `/research/outcome-validation` is an explicit single-symbol action; it never calls brokers or trains the active model. The dashboard distinguishes this from the separate rule-parameter Walk-Forward Optimizer.

`ResearchJobs` scans all configured symbols, attaches evidence, rechecks candle freshness at completion and reads, and defaults to blocking unvalidated sizing. Qualified rule estimates rank by estimated net expectancy; other scores are exploratory. A long scan may outlive minute-candle eligibility. Incremental feature caches/stream-driven recomputation are not implemented.

Training utilities use shared timestamp boundaries across symbols, purge labels whose outcome crosses a partition, and reserve separate tuning windows for early stopping/threshold selection. Direction/cost targets exclude unknown futures; enhanced barrier labels follow the full observed path. Rolling regime ranks replace full-history quantiles. These repairs do not validate or automatically replace the existing ML artifact.

## System at a glance

```text
                       +-------------------+
Kite REST/WebSocket -->| ingest            |--> Parquet candles
RSS/news ------------->|                   |--> caches
                       +---------+---------+
                                 |
                     +-----------v-----------+
                     | indicators + signals  |
                     +-----------+-----------+
                                 |
                +----------------+----------------+
                | rule | ML | optional LLM | shadow|
                +----------------+----------------+
                                 |
                    opportunities + paper risk
                                 |
                    SQLite <--> FastAPI <--> React
```

The system is local-first and paper-only. It can be useful in three independent modes:

1. stored-data research and backtests;
2. API/dashboard over existing data;
3. market-hours ingestion and scheduled paper analysis.

## Components

### Restored research dashboard

`frontend/src/ResearchDashboard.tsx` is the entry point. It reuses the original Plotly `PriceChart` and panel/layout styling; `OperatingConsole` remains a separate paper-operations tab. New research contracts are versioned under `/api/v1/research`.

| Component | Responsibility |
|---|---|
| `strategy/research_features.py` | Original nine-feature formulas and trusted local classifier adapter; explicit schema/class/interval checks |
| `analysis/research_engine.py` | Completed-candle rule/ML decisions, conflict handling, timestamped evidence, chart payloads and portfolio-bounded plans |
| `analysis/research_jobs.py` | Full-universe background scan, progress/skips/errors, cancellation and atomic snapshot; one API worker |
| `analysis/research_news.py` | On-demand ticker news, validated optional LLM sentiment, persistent cache and temporal eligibility |
| `api/routers/research.py` | Scan/status/cancel, selected-symbol chart/evidence and explicit news actions |

No inference path retrains or overwrites models. No scan makes broker requests, paid AI calls or paper entries. Full scans include every unique configured symbol; the displayed top lists are derived only after coverage is known. Missing data/model/news is not a substitute BUY signal. The old compatibility helpers are not the dashboard's primary engine.

### `core`

Pure or mostly pure shared primitives: quantitative indicators, pattern detection, signal evaluation, position sizing, configuration, logging, freshness, shutdown, and circuit breakers.

### `ingest`

- Kite credential and token management
- instrument lookup/cache
- historical REST downloads
- live WebSocket aggregation
- watchlist resolution
- news collection and gap-healing support

The daily Kite token is stored at `data/.access_token` so local and Docker services share one location.

### `storage`

- SQLAlchemy models and async repositories for relational state
- direct SQLite compatibility functions used by monitoring/legacy modules
- partitioned Parquet candle storage
- Alembic migrations

SQLite is the supported relational backend. Parquet remains the source for bulk candle analytics.

### `strategy`

`CandidateGenerator` prepares current indicators and a rule action. It exposes two deliberate entry points:

- `generate_for_symbol`: read the newest stored candles for live/current work;
- `generate_from_data`: operate only on a caller-supplied frame for backtests and time-bounded jobs.

Rule, ML, LLM, hybrid, and shadow modules consume candidates. External LLM use is explicit; mock is the default.

### `research` and `optimize`

Backtests, edge diagnosis, strategy experiments, and walk-forward optimization. A backtest owns its historical data window and passes slices into strategy evaluation to avoid future leakage.

### `risk`

Paper-wallet accounting, portfolio exposure/risk constraints, and open-position monitoring. Stop/target evaluation uses candle extremes; monitoring writes recommendations/alerts and does not place broker orders.

### `api`

FastAPI application with routers for health, symbols/history, opportunities, paper positions/wallet, watchlists, pipeline control, digests, and liveness. The application lifespan initializes missing relational tables and closes engines on shutdown.

### `frontend`

React + TypeScript, built with Vite. `OperatingConsole.tsx` consumes same-origin `/api/v1` contracts. Local development uses a Vite proxy to `127.0.0.1:8000`; Docker serves static assets with unprivileged Nginx and proxies to `api:8000`. Legacy screens remain unmounted until their contracts are migrated.

### `scripts`

Long-running or maintenance orchestration:

- scheduler using the Asia/Kolkata timezone;
- setup validation and health monitoring;
- model drift/retraining;
- pipeline/supervisor helpers;
- timestamp migration.

## Main flows

### Historical-data flow

```text
CLI ingest -> Kite auth -> instrument token -> chunked REST requests
           -> normalized timestamps/OHLCV -> Parquet partitions
```

### Live flow

```text
trading-agent-ticker -> Kite WebSocket -> interval buckets -> Parquet
trading-agent-scheduler -> candidates/decisions/monitoring -> SQLite
```

### API/dashboard flow

```text
browser -> Vite (local) / Nginx (Docker) -> /api/v1 proxy -> FastAPI :8000
FastAPI -> SQLite repositories and/or Parquet reads -> JSON
```

### Backtest flow

```text
Parquet range -> chronological timestamps -> historical slice
              -> generate_from_data -> simulated position/exit
              -> cost and performance metrics
```

## Persistence contracts

### Runtime directories

| Path | Contents |
|---|---|
| `data/trading.db` | SQLite relational database |
| `data/parquet` | candle partitions |
| `data/.access_token` | current Kite token; secret and Git-ignored |
| `data/cache` or `.cache` | instrument/derived caches |
| `models` | model files and registry |

### Important tables

| Table | Purpose |
|---|---|
| `watchlist` | active symbols and instrument metadata |
| `candles` | legacy relational candles; monitor prefers canonical Parquet |
| `signals` | independent strategy-tagged decisions |
| `open_positions` | canonical open AND closed paper positions, risk fields, and gross realized PnL |
| `position_alerts` | monitor findings |
| `trades` | legacy/research trade records; not the manual console's accounting source |
| `opportunities` | ranked candidates |
| `ml_models` | model registry metadata |
| `wallet` | legacy/research snapshots; API derives current book from position records |
| `system_events` | operational events |

SQLAlchemy/Alembic names must remain aligned. API compatibility properties can expose older names without changing physical columns.

Parquet writes publish immutable batches under a symbol/interval OS lock. Reads normalize timestamps to UTC, deduplicate corrections, then filter. Compaction publishes replacement data before deleting its captured inputs. Catch-up replays a one-day overlap. These are single-host contracts, not a distributed database design.

The API serializes paper entry checks with SQLite `BEGIN IMMEDIATE`. Gross exposure, stop risk, position count, duplicate symbols, bracket direction, and the entry switch are checked before insert. Conditional close updates prevent duplicate close writes. Initial capital remains configuration, not a fully audited cash ledger. See [READINESS.md](READINESS.md) for the remaining financial/security/reliability gates.

## Startup and shutdown

### Local

The API starts through `trading-agent-api`. Missing tables are created during lifespan startup. Shutdown callbacks run before engines are disposed.

### Docker

```text
api (:8000, health checked) <--- frontend (:5173)
          ^
          +--- shared ./data and ./models --- ticker [live]
                                             scheduler [live]

tools [one-shot] uses the same mounts
```

The default stack does not start the live profile. There are no unused Redis/PostgreSQL containers.

## Failure behavior

- External requests use retries/circuit breakers where implemented.
- Missing market data produces an empty/no-data result rather than a fabricated price.
- The position monitor records alerts idempotently by position/kind/candle.
- Missing LLM credentials fall back to the mock client and log a warning.
- Missing Kite credentials prevent broker operations but do not prevent API/research startup.
- Scheduler job failures are surfaced by an APScheduler listener and do not silently stop unrelated jobs.

## Known constraints

The research dashboard also exposes a bounded `POST /api/v1/research/walk-forward` action. The rule optimizer shares the restored causal feature formulas; it uses rolling training windows, purged forward outcomes, disjoint test windows, next-bar entries and the same costs in train/test. Only parameters used by neutral-news rules are searched. It does not train/promote ML artifacts or write positions. One synchronous worker-thread calculation is allowed at a time per API process; use one API worker. Results are returned to the page, not durably scheduled. See the user guide for statistical and execution limitations.

- SQLite permits limited write concurrency; avoid duplicate ticker/scheduler processes.
- Some older operational tables described in historical research documents are not first-class SQLAlchemy models.
- The Plotly bundle is large and causes a frontend build warning.
- Automatic Kite login depends on broker page stability; manual login remains the fallback.
- No real-order execution or broker position reconciliation exists.

## Validation

```bash
pytest
python -m compileall -q src migrations
docker compose config
cd frontend && npm run build
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for binding decisions and [USER_GUIDE.md](USER_GUIDE.md) for operations.
