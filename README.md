# Trading Agent

Trading Agent is a local-first research and paper-trading system for NSE intraday data. It can ingest Kite Connect candles, store time-series data in Parquet, evaluate rule/ML/LLM decision paths, backtest strategies, monitor paper positions, and expose the results through a FastAPI API and React dashboard.

> This project does not place broker orders. It is research and paper-trading software, not financial advice.

## What works without broker credentials

The API, dashboard, tests, stored-data analysis, and backtests can run without Kite credentials. Kite credentials and a current daily access token are needed only for downloading or streaming broker data.

## Quick start: local

Use Python 3.11 or 3.12. Python 3.13 also passes the current test suite, but 3.11 is the version used by Docker and CI.

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

Then open http://localhost:5173. Vite proxies `/api` to the backend on port 8000.

## Quick start: Docker

Docker Compose uses the same SQLite/Parquet storage model as local execution. `./data` and `./models` are mounted into every Python service.

```bash
cp .env.example .env       # PowerShell: Copy-Item .env.example .env
docker compose up --build -d
docker compose ps
```

Open the dashboard at http://localhost:5173 or the API docs at http://localhost:8000/docs.

Useful Docker commands:

```bash
# Run any CLI command in a temporary tools container
docker compose run --rm tools trading-agent --help
docker compose run --rm tools trading-agent storage-stats
docker compose run --rm tools trading-agent ingest RELIANCE TCS --days 30

# Start the market-hours ticker and scheduler profile
docker compose --profile live up -d ticker scheduler

# Follow logs and stop the stack
docker compose logs -f api
docker compose down
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
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8000` | API bind address |
| `API_CORS_ORIGINS` | local frontend origins | JSON list of allowed origins |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `console` | Logging behavior |
| `LLM_PROVIDER` | `mock` | `mock`, `openai`, or `anthropic` |
| `LLM_BASE_URL` | OpenAI | Base URL for an OpenAI-compatible API |
| `LLM_API_KEY` / `LLM_MODEL` | empty / `gpt-4o-mini` | Optional LLM credentials/model |

Keep `LLM_PROVIDER=mock` to guarantee that the comparison path makes no external LLM calls. See [.env.example](.env.example) for every supported setting.

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
```

Do not edit an already-applied initial migration in a deployed database; add a new Alembic revision instead.

## Development checks

```bash
pytest
pytest --cov=trading_agent --cov-report=term-missing
ruff check src migrations

cd frontend
npm run typecheck
npm run build
```

The current automated suite contains 156 tests. The exact count may change, so treat a zero exit code as the source of truth.

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

## Documentation map

- [USER_GUIDE.md](USER_GUIDE.md): detailed setup, daily operation, and recovery steps
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
