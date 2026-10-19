# Trading Agent

Trading Agent is a local-first research and paper-trading system for NSE intraday data. It can ingest Kite Connect candles, store time-series data in Parquet, evaluate rule/ML/LLM decision paths, backtest strategies, monitor paper positions, and expose the results through a FastAPI API and React dashboard.

> This project does not place broker orders. It is research and paper-trading software, not financial advice.

The supported operating surface is the versioned API and paper-operations console. This is **not yet an institutional live-trading platform**. See [READINESS.md](READINESS.md) for the integration audit, tested controls, and remaining release gates.

## What works without broker credentials

The API, dashboard, tests, stored-data analysis, and backtests can run without Kite credentials. Kite credentials and a current daily access token are needed only for downloading or streaming broker data.

## Quick start: local

Use Python 3.11 or 3.12 and Node 22 for the frontend. Python 3.13 is also tested locally; Docker and CI use Python 3.11. Only copy `.env.example` on first setup: do not overwrite your existing configuration.

```powershell
# Windows PowerShell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
Copy-Item .env.example .env

# Start the API; the SQLite schema is created automatically.
trading-agent-api
```

```bash
# macOS/Linux
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
cp .env.example .env
trading-agent-api
```

Open:

- API: http://localhost:8000
- Swagger API documentation: http://localhost:8000/docs
- Health check: http://localhost:8000/api/v1/health

To run the dashboard in a second terminal:

```bash
cd frontend
npm ci
npm run dev
```

Then open http://localhost:5173. Vite proxies `/api` to the backend on port 8000. On PowerShell, use `npm.cmd` if execution policy blocks `npm.ps1`.

## Quick start: Docker

Docker Compose uses the same SQLite/Parquet storage model as local execution. `./data` and `./models` are mounted into every Python service. The frontend is a compiled bundle served by unprivileged Nginx, not a development server. Host ports are bound to loopback only.

```bash
cp .env.example .env       # PowerShell: Copy-Item .env.example .env
docker compose --profile live build
docker compose up -d api frontend
docker compose ps
```

Open the dashboard at http://localhost:5173 or the API docs at http://localhost:8000/docs.

The build command is required on first setup and after pulling or making application/dependency changes. It is not a daily command.

## Docker operating schedule

These are the commands an operator may need to repeat. Run them from the repository root.

| When | Required? | Command | Purpose |
|---|---|---|---|
| Before the first market-data operation of each trading day, or whenever Kite rejects the token | Yes, for broker data | `docker compose run --rm tools trading-agent login` | Automatically signs in and saves the current token in `data/.access_token` |
| After login on first use, after downtime, or when candles are missing | As needed | See **Catch up missing candles** below | Replays a one-day overlap and catches up the selected interval |
| Whenever the dashboard/API should run | Yes | `docker compose up -d api frontend` | Starts the API and dashboard without live broker processes |
| At the start of a live market-data session | Yes, for live mode | `docker compose --profile live up -d api frontend ticker scheduler` | Starts the complete application, live ticker, and scheduler |
| Immediately after startup | Recommended | `docker compose ps` | Confirms that the requested services started and the API is healthy |
| While monitoring or troubleshooting | Optional | `docker compose logs -f --tail=200 api frontend ticker scheduler` | Streams logs; press `Ctrl+C` to leave the log view without stopping containers |
| At the end of a session or before maintenance | Optional | `docker compose down` | Stops containers without deleting candle data, models, or the access token |
| After source, dependency, Dockerfile, or Compose changes | Required after such changes | `docker compose --profile live build` | Rebuilds images; rerun the appropriate `up -d` command afterward to replace old containers |

### Normal trading-day sequence

Authenticate first, catch up missing candles if the application was not continuously running, and then start live mode:

```bash
docker compose run --rm tools trading-agent login

# Run the catch-up command from the next section when needed.

docker compose --profile live up -d api frontend ticker scheduler
docker compose ps
docker compose logs -f --tail=200 api frontend ticker scheduler
```

The login command reuses a still-valid token when possible and automatically logs in when a new token is required. `Ctrl+C` only exits the final log-view command; the application continues running in Docker.

### Catch up missing candles

Run this after login when the system has been offline or a download was interrupted. Start with 100 symbols, review the summary, then omit `--limit` to process the complete watchlist. Choose the **same interval as `STORAGE_CANDLE_INTERVAL`** (default `5minute`); one-minute and five-minute datasets are separate and are not automatically resampled.

The command replays one day before each latest stored candle, correcting recent partial live bars and catching up through the last completed bar. A new symbol defaults to 30 days. Reads deduplicate retries by timestamp. This is not a complete interior-gap audit: use an explicit historical ingest for older holes. Physical storage row counts include replay copies until compaction.

Symbols that are no longer present in Kite's current NSE equity instrument list are logged as `gap_heal_symbol_skipped` and do not stop the remaining watchlist from being processed.

```bash
# Bounded validation first; replace 5minute with minute if configured that way.
docker compose run --rm tools trading-agent heal-gaps --interval 5minute --limit 100
# Full watchlist, only when needed:
docker compose run --rm tools trading-agent heal-gaps --interval 5minute
```

For a long run in Windows PowerShell, save the output so progress and failures can be reviewed later:

```powershell
docker compose run --rm tools trading-agent heal-gaps --interval 5minute --limit 100 *> data\backfill_latest.log
$LASTEXITCODE
Get-Content data\backfill_latest.log -Tail 100
```

If interrupted, rerun after checking authentication. Exit code 1 means failure; inspect `errors` and logs. Invalid instruments are listed under `skipped`; `rows_written` includes overlap, not just newly discovered candles. Authentication or open-breaker failures stop the batch. Investigate the first error before retrying; run only one historical-data job at a time because pacing is process-local.

### Other occasional Docker commands

```bash
# Run any CLI command in a temporary tools container
docker compose run --rm tools trading-agent --help
docker compose run --rm tools trading-agent storage-stats
docker compose run --rm tools trading-agent ingest RELIANCE TCS --days 30

# Inspect a specific service after an error
docker compose logs --tail=200 api
```

The `live` profile is deliberately opt-in because it uses broker/network resources and starts scheduled jobs.

## Kite setup and daily authentication

1. Copy `.env.example` to `.env`.
2. Set `KITE_API_KEY`, `KITE_API_SECRET`, `KITE_USER_ID`, `KITE_PASSWORD`, `KITE_TOTP_SECRET`, and the redirect URL registered with Kite.
3. Authenticate each trading day:

```bash
# Automated TOTP/Playwright flow
trading-agent login

# Manual browser flow if automation is unavailable
python manual_login.py
```

The generated token is stored at `data/.access_token`, which is ignored by Git and shared with Docker through the data mount.

For automatic login, install Chromium locally once:

```bash
playwright install chromium
```

## Common workflow

```bash
# Download history. Omitting symbols uses the configured/default watchlist.
trading-agent ingest RELIANCE TCS INFY --days 100
trading-agent backfill RELIANCE --years 2
trading-agent storage-stats

# Research stored data
trading-agent diagnose RELIANCE TCS
trading-agent backtest RELIANCE TCS --start 2025-01-01 --end 2025-06-30
trading-agent optimize walk-forward RELIANCE TCS
trading-agent optimize ml-model-v2 RELIANCE TCS

# One pipeline pass
trading-agent pipeline RELIANCE TCS

# Long-running market-hours processes
trading-agent-ticker
trading-agent-scheduler
```

Run `trading-agent --help` or `trading-agent COMMAND --help` for the exact options. `python -m trading_agent.cli` is an equivalent fallback when console scripts are not on `PATH`.

## Configuration

Settings come from process environment variables first, then `.env`. Important values are:

| Variable | Default | Purpose |
|---|---|---|
| `DB_URL` | `sqlite:///data/trading.db` | SQLAlchemy database URL |
| `DATA_DIR` | `data` | Runtime data and access-token directory |
| `STORAGE_PARQUET_ROOT` | `data/parquet` | Candle storage |
| `ML_MODEL_DIR` | `models` | Model registry and artifacts |
| `API_HOST` / `API_PORT` | `127.0.0.1` / `8000` | Local bind address; Compose overrides inside its containers |
| `API_AUTH_TOKEN` | empty | Optional bearer authentication; enable before sharing access |
| `STORAGE_CANDLE_INTERVAL` | `5minute` | Ticker, scheduler, and position-monitor interval |
| `PAPER_INITIAL_CAPITAL` | `1000000` | Initial paper capital; keep fixed for an existing ledger |
| `PAPER_ENTRIES_ENABLED` | `true` | Set `false` and restart API to block new paper entries (closing remains allowed) |
| `PAPER_MAX_POSITIONS` | `10` | Maximum open paper positions |
| `PAPER_MAX_EXPOSURE_PCT` / `PAPER_MAX_RISK_PCT` | `0.50` / `0.06` | Aggregate notional and stop-risk fractions of gross book equity |
| `PAPER_MAX_TRADE_RISK_PCT` | `0.02` | Maximum stop risk per paper entry |
| `API_CORS_ORIGINS` | local frontend origins | JSON list of allowed origins |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `console` | Logging behavior |
| `LLM_PROVIDER` | `mock` | `mock`, `openai`, or `anthropic` |
| `LLM_BASE_URL` | OpenAI | Base URL for an OpenAI-compatible API |
| `LLM_API_KEY` / `LLM_MODEL` | empty / `gpt-4o-mini` | Optional LLM credentials/model |

Keep `LLM_PROVIDER=mock` to guarantee that the comparison path makes no external LLM calls. See [.env.example](.env.example) for every supported setting.

The dashboard accepts `API_AUTH_TOKEN` in its access-token form and keeps it in memory only. Do not put secrets in frontend build variables. With authentication enabled, browser Swagger access also requires an Authorization header; ordinary direct navigation does not supply one. Remote access additionally needs TLS, an authenticated gateway, and an operator/security review.

Paper wallet totals come from persisted `open_positions` records, including closed rows. They are gross entry-cost accounting: no live mark-to-market, fees, slippage, deposits, or broker reconciliation. Manual closes use the price you enter. Stops generate advisory alerts, not guaranteed fills or automatic broker exits. Do not change initial capital to conceal losses.

## Architecture

```text
Kite/RSS -> ingest -> Parquet + SQLite
                         |
              core indicators/signals
                         |
        strategy (rule / ML / optional LLM)
                         |
             analysis + paper-risk layer
                         |
                 FastAPI -> React UI
```

The source layout is:

```text
src/trading_agent/
  api/        FastAPI app and routers
  analysis/   rankings, opportunities, sentiment, digests
  core/       config, indicators, signals, logging, resilience
  ingest/     Kite auth/history/live ticker, news, watchlists
  optimize/   optimization CLI
  research/   backtests, diagnostics, walk-forward studies
  risk/       paper wallet, portfolio limits, position monitor
  scripts/    scheduler, health checks, maintenance
  storage/    SQLAlchemy schema and Parquet store
  strategy/   candidate generation and decision paths
frontend/     React + TypeScript dashboard
migrations/   Alembic database migrations
tests/        unit and integration tests
```

SQLite is the supported runtime database today. PostgreSQL is not advertised until the remaining direct `sqlite3` consumers are migrated.

## Database lifecycle

The API creates missing tables on startup for a zero-setup local experience. For explicit migration-controlled environments, run:

```bash
alembic upgrade head
alembic current
# Docker equivalent (new databases / migration-managed deployments):
docker compose run --rm tools alembic upgrade head
```

Do not edit an already-applied initial migration in a deployed database; add a new Alembic revision instead.

For a new migration-managed database, run migrations **before** starting the API. An older auto-created database may have tables but no Alembic revision: back it up and compare its schema before adopting migrations; do not blindly stamp, drop tables, or run the initial creation over it. Stop writers before filesystem backups and verify restoration into a separate directory. `docker compose down` preserves bind-mounted data; image pruning is not required for an upgrade.

## Development checks

```bash
pytest
pytest --cov=trading_agent --cov-report=term-missing
ruff check src migrations

cd frontend
npm run typecheck
npm run build
```

The suite includes storage replay/concurrency, broker-message normalization, paper-ledger/API contracts, and fresh-database migration checks. See [READINESS.md](READINESS.md) for validation evidence and limitations; a build alone is not a live-session test.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `trading-agent` is not found | Activate the virtual environment and run `pip install -e .`, or use `python -m trading_agent.cli` |
| `No data available` | Authenticate, run `trading-agent ingest SYMBOL`, then check `trading-agent storage-stats` |
| Kite token rejected | Run `trading-agent login` again; Kite access tokens are short-lived |
| Playwright browser missing | Run `playwright install chromium`, or use `python manual_login.py` |
| Port 8000/5173 is busy | Change the port mapping in `docker-compose.yml` or use CLI/API port options |
| Docker frontend cannot reach API | Confirm `api` is healthy with `docker compose ps` and inspect `docker compose logs api` |
| SQLite is locked | Avoid multiple ad-hoc writers; stop duplicate ticker/scheduler processes |
| Rebuilt code appears unchanged | Rerun `docker compose up -d api frontend` (and live services if used); rebuilding alone does not replace running containers |
| `kite_historical` breaker is OPEN | Review the first underlying failure, check login, wait for broker recovery, then retry a bounded batch; avoid concurrent backfills |
| API returns 401 | Enter the configured bearer token in the console; never disable authentication to expose a public service |
| Paper entry returns 409 | Read the error: duplicate symbol, entry switch, capital, position, exposure, or stop-risk limit; closes remain available |

## Documentation map

- [USER_GUIDE.md](USER_GUIDE.md): detailed setup, daily operation, and recovery steps
- [READINESS.md](READINESS.md): integration audit, evidence, and institutional-readiness gates
- [ARCHITECTURE.md](ARCHITECTURE.md): architectural decisions
- [DESIGN.md](DESIGN.md): components, data flow, and deployment design
- [STRATEGIES.md](STRATEGIES.md): strategy hypotheses and promotion rules
- [EDGE_ANALYSIS.md](EDGE_ANALYSIS.md): measured signal-edge research
- [ML_REDESIGN_SUMMARY.md](ML_REDESIGN_SUMMARY.md): ML v2 design notes
- [PRD.md](PRD.md): product goals and constraints
- [RULES.md](RULES.md): contributor rules
- [TASKS.md](TASKS.md): active work and known limitations
- [MEMORY.md](MEMORY.md): historical decision log
- [TASKS_INTEGRATION.md](TASKS_INTEGRATION.md): archived integration implementation brief

## License

No license file is currently included. Treat the repository as all-rights-reserved unless the owner adds an explicit license.
