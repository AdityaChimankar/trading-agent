# Project decision log

This is a concise historical log. Current operating instructions live in [README.md](README.md) and [USER_GUIDE.md](USER_GUIDE.md); binding decisions live in [ARCHITECTURE.md](ARCHITECTURE.md).

## 2026-10-19: Paper operating-path hardening

The follow-up audit found destructive partition overwrites, incorrect live volume/bucket handling, disconnected wallet/monitor data sources, dummy portfolio sizing, and incompatible frontend endpoints. The operating path now uses immutable Parquet batches, exchange-aware timestamps, persisted paper accounting, serialized entry caps, and a versioned console. Docker serves a compiled UI. Tests and unresolved institutional requirements are recorded in [READINESS.md](READINESS.md). No broker-order execution was added; live-session certification remains outstanding.

## 2026-10-19: End-to-end contract audit

Findings:

- nested settings did not read flat `.env` values consistently;
- a generic host `DEBUG=release` value could prevent any package import;
- Docker invoked nonexistent console modules and mounted Alembic Python files as PostgreSQL initialization SQL;
- the application still had direct SQLite consumers, so advertised PostgreSQL support was misleading;
- ORM, bootstrap SQL, and the initial migration disagreed on paper-position columns;
- scheduler wrappers passed symbol strings into APIs that require `Candidate` objects;
- an async news job was scheduled as a plain callable;
- backtests generated signals by reading the newest Parquet data during historical replay;
- backtest trades discarded stop/target values used by exit checks;
- ML retraining referenced APIs that no longer existed;
- the Docker Vite proxy targeted the frontend container itself.

Decisions:

- support SQLite explicitly until all direct consumers are migrated;
- align schema contracts and retain Python compatibility aliases;
- add `generate_from_data` for time-bounded research;
- make live Docker services opt-in profiles;
- default external LLM work to a mock client;
- use `data/.access_token` as the shared token location;
- keep one canonical pytest configuration in `pyproject.toml`.

Verification at the time of the audit: 156 backend tests passed and the frontend production build completed. Docker image build and a credentialed live session still require an appropriate external environment.

## 2026-10-18: Installable package and Docker image

Runtime modules were moved under `src/trading_agent`, with console entry points managed by `pyproject.toml`. A multi-stage Docker build and `.dockerignore` were added to keep build context and runtime layers bounded.

Lesson: entry-point declarations must be exercised after packaging; a module path existing conceptually is not enough.

## 2026-10-18: Integration layer

Portfolio exposure, position monitoring, opportunity exclusions, and API serialization were implemented against integration tests.

Lesson: keep the persisted schema and the test/runtime model contract identical. Compatibility belongs in aliases/serializers, not parallel table definitions.

## 2026-10-06: SQLite lock recovery

The live ticker added retry/connection recreation after lock failures.

Lesson: WAL/timeouts do not make SQLite a high-concurrency database. Coordinate writers and make transactions short.

## 2026-10-05: Edge diagnostics followed strategy changes

Edge diagnosis was updated when the underlying signal definition changed.

Lesson: research tools must import the same primitives as live decision paths or clearly declare intentional differences.

## 2024-08-15: Exit research

Historical experiments found that a fixed take-profit capped winners in the studied sample. The associated result is point-in-time evidence, not a guarantee; rerun it before relying on it.

Lesson: exit rules can dominate entry tweaks, and backtest/live exit semantics must match.

## Operational heuristics

When diagnosing a problem, check in this order:

1. `trading-agent --help` and configuration loading;
2. `GET /api/v1/health` then `/api/v1/ready`;
3. `trading-agent storage-stats` and the relevant Parquet symbol;
4. `data/.access_token` freshness for Kite work;
5. process/container logs;
6. duplicate writers and SQLite lock state;
7. model registry metadata for ML work.

Never infer that an empty dashboard means the API is broken; a fresh install legitimately has no candles or paper positions.
