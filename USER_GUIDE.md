# Trading Agent User Guide

This guide covers a first local run, Docker operation, the daily Kite workflow, research commands, and common recovery steps. For the shorter path, start with [README.md](README.md).

> Safety: the application records paper positions only. It has no real-order execution path.

## 1. Choose how to run it

| Mode | Best for | Requirements |
|---|---|---|
| Local Python | development, research, debugging | Python 3.11/3.12; Node 20+ for UI |
| Docker Compose | repeatable API/UI and live services | Docker Engine/Desktop with Compose v2 |

Both modes use SQLite at `data/trading.db`, Parquet below `data/parquet`, models below `models`, and the token at `data/.access_token`.

## 2. Local installation

From the repository root:

```powershell
# Windows PowerShell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
Copy-Item .env.example .env
```

```bash
# macOS/Linux
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
cp .env.example .env
```

Verify the installation:

```bash
trading-agent --help
python -c "import trading_agent; print(trading_agent.__version__)"
pytest
```

If the console command is not on `PATH`, use `python -m trading_agent.cli` in its place.

## 3. Configuration

Edit `.env`. The API and dashboard need no secrets. Broker ingestion requires at least:

```dotenv
KITE_API_KEY=replace_me
KITE_API_SECRET=replace_me
KITE_USER_ID=replace_me
KITE_PASSWORD=replace_me
KITE_TOTP_SECRET=replace_me
KITE_REDIRECT_URL=http://localhost
```

Keep this local default unless you intentionally move the database:

```dotenv
DB_URL=sqlite:///data/trading.db
```

The optional LLM path defaults to a local mock and makes no paid call:

```dotenv
LLM_PROVIDER=mock
LLM_BASE_URL=https://api.openai.com
LLM_API_KEY=
LLM_MODEL=gpt-4o-mini
```

To use an OpenAI-compatible provider, set `LLM_PROVIDER=openai`, its base URL, key, and model. Never commit `.env` or `data/.access_token`.

## 4. Start the local application

Terminal 1:

```bash
trading-agent-api
```

Terminal 2:

```bash
cd frontend
npm ci
npm run dev
```

Check these URLs:

| URL | Expected result |
|---|---|
| http://localhost:8000/api/v1/health | JSON with `status: healthy` |
| http://localhost:8000/api/v1/ready | JSON with `status: ready` |
| http://localhost:8000/docs | Interactive API docs |
| http://localhost:5173 | Dashboard |

The API creates missing tables on startup. Alembic can be used explicitly with `alembic upgrade head` in migration-controlled deployments.

## 5. Authenticate with Kite

Kite access tokens generally need refreshing each trading day.

```bash
# Automated browser/TOTP path
playwright install chromium   # first use only
trading-agent login

# Manual fallback
python manual_login.py
```

Both paths save the token under `data/.access_token`. Do not paste a token into logs, issues, screenshots, or documentation.

## 6. Load market data

```bash
# Specific symbols
trading-agent ingest RELIANCE TCS INFY --days 100

# Default watchlist
trading-agent ingest

# Longer history
trading-agent backfill RELIANCE TCS --years 2

# Inspect local Parquet storage
trading-agent storage-stats
```

The default watchlist comes from `symbols.txt`; named Nifty 500 loading uses `symbols_nifty500_plus.txt`. Explicit symbols are safest for the first run and avoid a large broker request.

## 7. Research and analysis

Run these only after the selected symbols have local Parquet data:

```bash
trading-agent diagnose RELIANCE TCS
trading-agent backtest RELIANCE TCS --start 2025-01-01 --end 2025-06-30
trading-agent optimize walk-forward RELIANCE TCS
trading-agent optimize walk-forward-single RELIANCE
trading-agent optimize ml-model-v2 RELIANCE TCS
trading-agent optimize strategy-lab RELIANCE
```

Backtests now generate candidates from the time-bounded historical slice supplied by the engine; they do not read the latest future candle while replaying history.

The measured rule-based edge and strategy promotion constraints are documented in [EDGE_ANALYSIS.md](EDGE_ANALYSIS.md) and [STRATEGIES.md](STRATEGIES.md).

## 8. Run the paper-trading pipeline

For one pass:

```bash
trading-agent pipeline RELIANCE TCS
```

For market-hours operation, use separate terminals:

```bash
# Live Kite WebSocket -> Parquet candles
trading-agent-ticker

# News, decisions, monitoring, maintenance
trading-agent-scheduler

# API/dashboard backend
trading-agent-api
```

The scheduler uses the Asia/Kolkata timezone. It stores rule, ML, LLM, and shadow outputs independently so the paths can be compared. With `LLM_PROVIDER=mock`, the LLM path records mock HOLD decisions without a network charge.

## 9. Docker Compose

Prepare configuration first:

```bash
cp .env.example .env       # PowerShell: Copy-Item .env.example .env
docker compose up --build -d
docker compose ps
```

The default stack contains:

| Service | Purpose | Port/profile |
|---|---|---|
| `api` | FastAPI backend | 8000 |
| `frontend` | Vite dashboard | 5173 |
| `tools` | one-shot CLI container | `tools` profile / `docker compose run` |
| `ticker` | live Kite stream | `live` profile |
| `scheduler` | scheduled pipeline | `live` profile |

Common commands:

```bash
# CLI tasks
docker compose run --rm tools trading-agent --help
docker compose run --rm tools trading-agent login
docker compose run --rm tools trading-agent ingest RELIANCE TCS --days 100
docker compose run --rm tools trading-agent backtest RELIANCE --start 2025-01-01 --end 2025-06-30

# Enable long-running broker services only when ready
docker compose --profile live up -d ticker scheduler

# Observe and stop
docker compose logs -f api
docker compose logs -f ticker scheduler
docker compose down
```

`./data` and `./models` are bind-mounted. Stopping or rebuilding containers does not remove those host files. Docker Compose no longer starts unused PostgreSQL or Redis services; the application still has direct SQLite consumers and therefore officially supports SQLite.

## 10. CLI reference

| Command | Purpose |
|---|---|
| `trading-agent login` | Refresh Kite token using the automated flow |
| `trading-agent ingest [SYMBOL...]` | Fetch recent historical candles |
| `trading-agent backfill [SYMBOL...]` | Fetch a longer history |
| `trading-agent storage-stats` | Summarize Parquet data |
| `trading-agent compact SYMBOL` | Compact a symbol's Parquet data |
| `trading-agent diagnose [SYMBOL...]` | Compare signal behavior with baselines |
| `trading-agent backtest SYMBOL...` | Replay a period using stored data |
| `trading-agent pipeline [SYMBOL...]` | Run candidate/opportunity processing once |
| `trading-agent news [SYMBOL...]` | Fetch current financial headlines |
| `trading-agent api` | Start the API through the main CLI |
| `trading-agent optimize ...` | Walk-forward, ML, and strategy-lab commands |

Always use `--help` on a command before a long or quota-consuming run.

## 11. Data and schema

- SQLite: configuration, paper positions, signals, model metadata, and monitoring records.
- Parquet: historical and live OHLCV candle series.
- Model directory: serialized ML models plus `registry.json`.
- Cache directory: downloaded instruments and derived caches.

Fresh databases can be created either by API startup or by:

```bash
alembic upgrade head
```

If an initial migration has already been applied elsewhere, create a new revision for schema changes rather than rewriting history.

## 12. Tests and developer checks

```bash
# Backend
pytest
pytest --cov=trading_agent --cov-report=html
ruff check src migrations

# Frontend
cd frontend
npm run typecheck
npm run build
```

CI uses Python 3.11, SQLite, the backend tests, and the frontend production build.

## 13. Troubleshooting

### Import or command not found

Activate the intended virtual environment and reinstall from the repository root:

```bash
pip install -e ".[dev]"
python -m trading_agent.cli --help
```

### API is healthy but dashboard is empty

This is normal on a fresh installation. Authenticate and ingest one or two explicit symbols, then reload the dashboard.

### Readiness says database is unavailable

Confirm `DB_URL=sqlite:///data/trading.db`, the `data` directory is writable, and only one intended stack is running. The readiness query uses SQLAlchemy 2-compatible SQL.

### Kite login fails

- Confirm every `KITE_*` value is set and the redirect URL exactly matches the Kite app.
- Run `playwright install chromium` for automatic login.
- Use `python manual_login.py` when browser automation changes or is blocked.
- Generate a new request token; request tokens expire quickly.

### Docker service exits

```bash
docker compose ps -a
docker compose logs api
docker compose run --rm tools trading-agent --help
```

If `ticker` exits, refresh the Kite token and restart only that service.

### SQLite lock errors

Stop duplicate local/Docker schedulers or tickers. Keep the database on a local disk, not a network share. PostgreSQL is not yet a drop-in replacement because maintenance and health modules still use SQLite-specific queries.

### Frontend build warning about a large chart chunk

The build succeeds; Plotly is the large lazy-loaded chart dependency. It is a performance optimization opportunity, not a startup failure.

## 14. Safe shutdown

Use `Ctrl+C` for local processes. For Docker:

```bash
docker compose down
```

Avoid `docker compose down -v` unless you intentionally want to remove Docker-managed volumes such as frontend dependencies. Project data is stored in bind-mounted `./data` and `./models`; back those up before manual deletion.
