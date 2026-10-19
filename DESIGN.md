# System design

Last reviewed: 2026-10-19

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

React + TypeScript + Vite. Browser calls use same-origin `/api` paths. The local Vite proxy targets `127.0.0.1:8000`; Docker injects `VITE_PROXY_TARGET=http://api:8000`.

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
browser -> Vite :5173 -> /api proxy -> FastAPI :8000
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
| `candles` | relational candle access where needed |
| `signals` | independent strategy-tagged decisions |
| `open_positions` | paper positions and risk fields |
| `position_alerts` | monitor findings |
| `trades` | completed paper trades |
| `opportunities` | ranked candidates |
| `ml_models` | model registry metadata |
| `wallet` | paper account snapshots |
| `system_events` | operational events |

SQLAlchemy/Alembic names must remain aligned. API compatibility properties can expose older names without changing physical columns.

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
