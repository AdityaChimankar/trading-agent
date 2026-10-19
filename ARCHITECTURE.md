# Architecture decisions

Last reviewed: 2026-10-19

This file records decisions that constrain future changes. [DESIGN.md](DESIGN.md) describes the current components; [README.md](README.md) describes how to run them.

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

The default LLM provider is `mock`. External LLM calls require explicit provider, URL, key, and model configuration.

## ADR-005: Time-bounded research inputs

Status: accepted

Backtests and historical replays must pass their current historical slice to candidate generation. They must never call a convenience function that reads the newest candles from disk while replaying an older timestamp.

`CandidateGenerator.generate_from_data` is the boundary for historical evaluation; `generate_for_symbol` is for current stored data.

## ADR-006: Paper trading only

Status: accepted

The repository does not place broker orders. Position and wallet records are simulations. Any future order-execution feature requires a separate security/risk ADR, explicit user controls, reconciliation, idempotency, and audit logging.

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

The active console targets `/api/v1`. Legacy frontend panels remain unmounted. The persistent paper book is derived from all `open_positions` rows; it is gross entry-cost accounting, not broker cash or mark-to-market. Risk admission and duplicate checks are serialized in SQLite. New entries can be disabled while exits remain available.

## ADR-012: Single-host candle durability and health

Status: accepted for local filesystems

Writers use immutable, atomically published Parquet batches under per-symbol/interval file locks. Readers deduplicate normalized UTC timestamps before filtering; compaction writes a replacement before removing old files. Shared ticker heartbeat is atomic JSON in `DATA_DIR`. One-day historical overlap repairs recent partial live bars. These controls do not imply interior-gap completeness, multi-host locking, or recovery guarantees.

## ADR-013: Explicit deployment boundary

Status: accepted

Docker runs a compiled static frontend behind Nginx; only loopback ports are exposed. Bearer authentication is optional for local use and required before sharing access. No fake deployment jobs or unsupported scheduling success responses are allowed. Institutional-readiness gates and verification evidence live in [READINESS.md](READINESS.md); live order routing requires a separate approved design.
