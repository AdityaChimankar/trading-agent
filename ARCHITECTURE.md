# Architecture decisions

Last reviewed: 2026-10-24

This file records decisions that constrain future changes. [DESIGN.md](DESIGN.md) describes the current components; [README.md](README.md) describes how to run them; [MEMORY.md](MEMORY.md) tracks status and the history behind these decisions.

## ADR-001: Installable `src` package

Status: accepted

All runtime Python code lives below `src/trading_agent` and uses absolute `trading_agent.*` imports. The package is installed with `pip install -e .` for development or `pip install .` in an image.

Consequences:

- Imports behave the same in tests, CLI commands, and containers.
- User-facing entry points are declared in `pyproject.toml`.
- Root-level scripts are compatibility helpers, not import roots.

## ADR-002: Local-first persistence

Status: accepted

SQLite stores relational state and Parquet stores candles. Runtime data lives below `data/`; model artifacts live below `models/`.

PostgreSQL is not currently a supported drop-in runtime. Some operational modules intentionally use direct `sqlite3` queries and SQLite date functions. A PostgreSQL migration must replace those consumers before a PostgreSQL service is restored to Compose.

Consequences:

- A clean local start has no database server prerequisite.
- Docker bind-mounts `data` and `models` for persistence.
- Multiple writers must remain modest and coordinated.

## ADR-003: One canonical relational schema

Status: accepted

SQLAlchemy models in `storage/db.py`, SQLite bootstrap behavior, and Alembic migrations must use the same physical column names. The canonical paper-position fields are:

```text
position_size, position_value, risk_amount, opened_at,
signal_source, status, exit_price, exit_time, pnl
```

The Python aliases `quantity`, `entry_time`, and `strategy` remain for API compatibility. Fresh local API startup may create missing tables; migration-controlled deployments use `alembic upgrade head`.

## ADR-004: Independent decision paths

Status: accepted

Rule, ML, optional LLM, and shadow/hybrid decisions remain identifiable. They may share prepared market data and risk primitives, but one path must not silently overwrite another path's evidence.

The rule agent is the validated path. ML and LLM agents run in parallel, write to their own tables and never override the rule signal; Rules + ML holds when ML is not available.

Revised 2026-10-23: OpenRouter is the only supported external LLM provider. The code default remains `mock` (no calls). Calls go through one process-wide rate limiter with retries on 429/5xx that honour `Retry-After`.

## ADR-005: Time-bounded research inputs

Status: accepted

Backtests and historical replays must pass their current historical slice to candidate generation. They must never call a convenience function that reads the newest candles from disk while replaying an older timestamp.

`CandidateGenerator.generate_from_data` is the boundary for historical evaluation; `generate_for_symbol` is for current stored data.

## ADR-006: Paper trading only

Status: superseded by ADR-014 (2026-10-23)

The repository did not place broker orders. ADR-014 adds a guarded order router that keeps paper as the default and meets this ADR's preconditions: explicit user controls, reconciliation, audit logging and pre-trade risk checks. Idempotent intent IDs remain open work.

## ADR-007: Market timezone

Status: accepted

Stored timestamps should be timezone-aware. Scheduled market jobs use `Asia/Kolkata`, regardless of the container host timezone.

## ADR-008: Docker profiles

Status: accepted; revised 2026-10-19

The default Compose stack starts only the API and frontend. Broker/network processes are opt-in under the `live` profile:

```text
default: api + frontend
live:    ticker + scheduler
tools:   one-shot CLI container
```

This prevents a normal dashboard start from consuming broker quota or starting scheduled analysis unexpectedly.

The Python image remains multi-stage:

| Stage | Purpose |
|---|---|
| `builder` | Build and install runtime Python dependencies |
| `playwright` | Install Chromium used by automatic Kite login |
| `runtime` | Copy installed packages/browser; run as non-root user |

Development dependencies are not installed in the runtime image.

## ADR-009: Configuration contract

Status: accepted

Settings are loaded in this order:

1. process environment;
2. `.env`;
3. code defaults.

Nested setting groups read `.env` directly, so flat names such as `DB_URL`, `API_PORT`, and `KITE_API_KEY` work consistently. The generic `DEBUG` value is parsed defensively because developer tools sometimes set it to strings such as `release`.

Secrets must never enter Git, logs, generated documentation, or container image layers.

## ADR-010: Test and CI contract

Status: accepted; revised 2026-10-19

The required functional gate is:

```bash
pytest
cd frontend && npm run build
```

CI runs on Python 3.11 with SQLite. Style/type tools are valuable, but their configured scope must reflect an achievable baseline; a permanently failing lint gate is not a gate.

## Dependency direction

The intended dependency flow is:

```text
api / cli / scripts
        |
analysis / strategy / ingest / risk / research
        |
core + storage
```

`core` should stay deterministic where possible. `storage` owns persistence. `api`, CLI, and scripts orchestrate work and should not duplicate domain algorithms.

## Adding a new decision

Add or revise an ADR when a change affects persistence format, live-vs-backtest parity, external calls, order safety, deployment topology, or strategy promotion rules. Routine refactors do not require an ADR.

## ADR-011: One supported operating contract

Status: accepted for local paper operations, 2026-10-19

The active console targets `/api/v1`. Revised 2026-10-23: the classic panels are mounted again through ADR-017; they are not a second source of trading decisions. The persistent paper book is derived from all `open_positions` rows; it is gross entry-cost accounting, not broker cash or mark-to-market. Risk admission and duplicate checks are serialized in SQLite. New entries can be disabled while exits remain available.

## ADR-012: Single-host candle durability and health

Status: accepted for local filesystems

Writers use immutable, atomically published Parquet batches under per-symbol/interval file locks. Readers deduplicate normalized UTC timestamps before filtering; compaction writes a replacement before removing old files. Shared ticker heartbeat is atomic JSON in `DATA_DIR`. One-day historical overlap repairs recent partial live bars. These controls do not imply interior-gap completeness, multi-host locking, or recovery guarantees.

## ADR-013: Explicit deployment boundary

Status: accepted

Docker runs a compiled static frontend behind Nginx; only loopback ports are exposed. Bearer authentication is optional for local use and required before sharing access. No fake deployment jobs or unsupported scheduling success responses are allowed. Institutional-readiness gates and verification evidence live in [READINESS.md](READINESS.md); live order routing follows ADR-014.

## ADR-014: Guarded order router

Status: accepted, 2026-10-23

Every entry and exit goes through `execution/router.py`. On entry, the router checks the participation cap, then reserves the position through the same serialized ledger transaction as manual entries. That transaction applies the kill switch, session gates, trade cap and ledger limits. Only then does it send the order, and it rewrites the position at the fill price. A broker rejection deletes the reservation. Risk checks therefore never run after a real fill.

Paper mode is the default and fills at the reference price moved by `EXECUTION_SLIPPAGE_BPS`. Live mode (Kite MARKET, product MIS) requires both `EXECUTION_MODE=live` and `EXECUTION_LIVE_CONFIRMATION=I-ACCEPT-REAL-MONEY-RISK`. Orders persist in the `orders` table (migration 003). Reconciliation compares ledger and broker net quantities.

Consequences: the router is the only code allowed to call a broker order API. Partial fills, cancels and reconnect replay are not yet handled; see [READINESS.md](READINESS.md#gates-before-institutional-or-unattended-live-use).

## ADR-015: Intraday session controls

Status: accepted, 2026-10-23

`risk/controls.py` owns:

- the kill switch, persisted in `DATA_DIR/controls`; an unreadable state fails closed;
- session gates: no entries before 09:15 IST, after `PAPER_ENTRY_CUTOFF` or at weekends;
- the daily trade cap;
- square-off of all open positions at `PAPER_SQUARE_OFF_TIME`, triggered by the scheduler;
- an append-only daily JSONL audit trail.

`PAPER_ENFORCE_SESSION=false` disables only the time gates and exists for tests. Exits are never blocked.

## ADR-016: Drift withdraws ML instead of degrading silently

Status: accepted, 2026-10-23

`strategy/model_monitoring.py` logs live ML inputs and compares them with a baseline (PSI and KS). When enough features drift, a persisted fallback makes the research scan use `ml_model.previous.joblib` if present, otherwise a withheld model that reports ML as unavailable, so hybrid decisions hold. An operator clears the fallback explicitly. Drifted ML must never keep voting unnoticed.

## ADR-017: One scan snapshot behind both UIs

Status: accepted, 2026-10-23

The classic React pages (restored from `gemini-old-interface`) and the research workspace run in one app (`frontend/src/App.tsx`, hash routes, Material UI theme). The classic `/api` routes in `api/routers/legacy_ui.py` adapt the research scan snapshot (`research_jobs.snapshot()`) to the old response shapes. They do not compute their own rankings. Rankings, opportunities, sizing and the pipeline therefore agree across pages.
