# Contributor rules

Last reviewed: 2026-10-19

## Safety and scope

- This repository is paper-trading and research software.
- Do not add broker order placement under the guise of a refactor.
- Never log or commit API keys, passwords, TOTP seeds, request tokens, access tokens, or account data.
- Keep `.env` and `data/.access_token` ignored.
- Validate strategy claims out of sample and after costs before changing any promotion status.

## Source layout

- Runtime modules belong under `src/trading_agent`.
- Use absolute `trading_agent.*` imports.
- Keep orchestration in API/CLI/scripts and reusable logic in domain modules.
- Avoid new root-level patch/helper scripts; add an installed CLI command or a package module.
- Frontend code belongs under `frontend/src` and talks to the backend through `/api`.

## Configuration

- Add settings to the correct `BaseSettings` group in `core/config.py`.
- Use the established flat environment prefix (`DB_`, `API_`, `KITE_`, `LLM_`, etc.).
- Document user-facing settings in `.env.example`, README, and the user guide.
- Configuration must not make package import fail because an unrelated generic environment variable exists.

## Data and migrations

- SQLite and Parquet are the supported stores.
- Keep SQLAlchemy, `init_db`, and Alembic physical column names aligned.
- Never edit an initial migration that has already shipped to a deployed database; add a new revision.
- Store timezone-aware timestamps; scheduler market rules use Asia/Kolkata.
- Treat Parquet candles as immutable time-series input except for explicit deduplication/compaction.
- Backtests must consume time-bounded frames and must not read the newest store state from inside a historical loop.

## Strategy discipline

- Keep rule, ML, LLM, hybrid, and shadow results attributable.
- A fallback must be explicit; do not disguise a mock result as a real model result.
- Use realistic costs and explain any fill assumptions.
- Compare entry logic with an unconditional/random baseline.
- Keep backtest and paper-monitor stop/target semantics aligned.
- Preserve conservative behavior for missing, stale, or invalid market data.

## Python style

- Target Python 3.11+.
- Prefer typed dataclasses/Pydantic models at boundaries.
- Use `pathlib.Path`, timezone-aware datetimes, and context managers.
- Raise domain-specific errors at external boundaries and retain the original exception as the cause.
- Do not perform network or expensive filesystem work at module import time.
- Public CLI commands must provide useful `--help` output and non-zero failure exits.

## API style

- Keep response models explicit.
- Health means the process is alive; readiness checks required dependencies.
- Use SQLAlchemy `text()` for textual SQL.
- Validate actions, prices, quantities, and ranges at request boundaries.
- A background task must update observable status on success and failure.

## Frontend style

- Keep API paths relative (`/api/...`).
- Configure the development proxy with `VITE_PROXY_TARGET`; do not bake container-only hostnames into browser code.
- Run TypeScript checking and the production build before merging UI changes.
- Large visualization dependencies should be lazy-loaded where practical.

## Docker

- Keep build context small through `.dockerignore`.
- Install runtime dependencies only in the final Python image.
- Run the application as a non-root user.
- Persist `data` and `models` through mounts.
- The default Compose profile must not start broker-connected or paid-call workloads.
- Do not reintroduce PostgreSQL/Redis services until runtime code uses them.

## Tests

Minimum functional checks:

```bash
pytest
python -m compileall -q src migrations
cd frontend && npm run build
```

Add a regression test for every fixed defect when the behavior is reasonably isolatable. Tests must use workspace/system temp facilities and must not depend on a developer's `.env`, account, network, or existing data.

Lint and typing are incremental quality tools. Tighten their CI gates only after the selected baseline passes; do not declare a known-failing job required.

## Documentation

- README is the canonical quick start.
- USER_GUIDE contains detailed operation and recovery.
- ARCHITECTURE records binding decisions.
- DESIGN explains the current implementation.
- Historical experiment documents must state that their numbers are point-in-time results.
- Never state a test count, image size, performance result, or strategy return as current unless it was just verified.
- Use `docker compose`, not the deprecated `docker-compose` form.

## Before a pull request

1. Review `git diff` for secrets and unrelated edits.
2. Run backend tests and frontend build.
3. Exercise changed CLI/API entry points.
4. Update `.env.example` and docs for any interface change.
5. Call out migrations, data compatibility, and operational steps in the PR description.
