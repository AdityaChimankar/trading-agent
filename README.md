# Trading Agent - NSE Intraday Trading Signal System

A local-first research pipeline for NSE intraday signals. It ingests live and historical candles, computes deterministic technical/pattern features, and runs **three independent decision paths side by side** — rule-based, LLM, and ML — so their calls can be compared on real outcomes instead of argued about.

**Status: ✅ 154/154 tests passing (including 3 integration test suites)**

## Architecture

```
trading-agent/
├── src/trading_agent/
│   ├── core/           # Shared types, config, indicators, logging, exceptions
│   ├── ingest/         # Data ingestion (Kite Connect, historical, live, news)
│   ├── storage/        # Database (SQLAlchemy) + Parquet storage
│   ├── strategy/       # Signals, candidates, decision agents (rule/LLM/ML)
│   ├── risk/           # Portfolio risk, position monitoring, paper wallet
│   ├── analysis/       # Opportunity finder, rankings, technical summary
│   ├── api/            # FastAPI REST API + WebSocket
│   ├── research/       # Backtesting, edge diagnosis, strategy lab
│   ├── optimize/       # Walk-forward optimization CLI
│   └── cli/            # Main CLI entry point
├── tests/              # Unit and integration tests (154 passing)
├── frontend/           # React + Vite dashboard
├── migrations/         # Alembic database migrations
├── docker-compose.yml  # Local development stack
├── Dockerfile          # Optimized multi-stage production image
└── .dockerignore       # Excludes data/, models/, node_modules/, caches
```

## Key Features

- **Multi-path Decision Making**: Rule-based, LLM, and ML signals run independently
- **Walk-Forward Optimization**: Proper out-of-sample parameter validation
- **Vectorized Backtesting**: Fast, NumPy-based simulation engine
- **Gap Healing**: Automatic detection and repair of missing candles
- **Paper Trading**: Full position management with realistic charges
- **Modern Stack**: Python 3.11+, FastAPI, SQLAlchemy 2.0, Polars, LightGBM
- **Type Safety**: Full type hints with mypy, ruff, black
- **Optimized Docker**: Multi-stage build (~2-3 GB final image vs 75 GB naive)
- **Integration Layer Complete**: Autologin, scheduler, portfolio risk, position monitor, opportunity finder all functional

## Quick Start

### Prerequisites

- Python 3.11+
- Kite Connect API subscription (₹2000/month base + ₹500/month for historical data)
- Git

### Installation

```bash
# Clone repository
git clone <repo-url>
cd trading-agent

# Create virtual environment
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/Mac:
source .venv/bin/activate

# Install package in editable mode
pip install -e ".[dev]"

# Install Playwright for auto-login (optional)
playwright install chromium

# Copy and configure environment
cp .env.example .env
# Edit .env with your Kite credentials
```

### Step-by-Step Usage Guide

#### 1. Configure Credentials

Edit `.env` with your Kite Connect credentials:

```env
# REQUIRED - Get from https://developers.kite.trade
KITE_API_KEY=your_key
KITE_API_SECRET=your_secret
KITE_TOTP_SECRET=your_32_char_base32_secret  # From 2FA QR code
KITE_USER_ID=your_user_id
KITE_PASSWORD=your_password
KITE_REDIRECT_URL=http://localhost

# OPTIONAL - For LLM/sentiment
OPENROUTER_API_KEY=your_openrouter_key
```

#### 2. Daily Login (Required - Token Expires at 7:30 AM IST)

**Option A: Manual Login Script (Recommended)**
```bash
python manual_login.py
```
This opens the login URL, you log in via browser with User ID + Password + TOTP, then paste the `request_token` back.

**Option B: CLI Command**
```bash
trading-agent login
```

**Option C: Docker**
```bash
docker compose run --rm ingest python manual_login.py
```

#### 3. Fetch Historical Data

```bash
# Fetch specific symbols (100 days)
trading-agent ingest RELIANCE TCS INFY --days 100

# Fetch entire watchlist
trading-agent ingest

# Backfill 2 years
trading-agent backfill --years 2

# Check storage
trading-agent storage-stats
```

#### 4. Run Analysis & Backtests

```bash
# Diagnose signal edge vs random baseline
trading-agent diagnose RELIANCE TCS

# Vectorized backtest
trading-agent backtest RELIANCE TCS --start 2024-01-01 --end 2024-06-30

# Walk-forward optimization
trading-agent optimize walk-forward RELIANCE TCS

# Train enhanced ML model (v2)
trading-agent optimize ml-model-v2 RELIANCE TCS

# Strategy lab grid search
trading-agent optimize strategy-lab RELIANCE
```

#### 5. Live Trading Pipeline (Market Hours: 9:15-15:30 IST)

```bash
# Terminal 1: Live ticker (WebSocket)
python -m trading_agent.ingest.live_ticker

# Terminal 2: Scheduler (news, decisions, position monitoring)
python -m trading_agent.scripts.scheduler

# Terminal 3: API server (for dashboard)
trading-agent api

# Terminal 4 (optional): Run pipeline once
trading-agent pipeline
```

#### 6. Docker Deployment (Optional)

```bash
# Build and start all services
docker compose up --build -d

# Daily login in container
docker compose run --rm ingest python manual_login.py

# Fetch data in container
docker compose run --rm ingest trading-agent ingest

# Access services:
# - API: http://localhost:8000
# - API Docs: http://localhost:8000/docs
# - Frontend: http://localhost:5173
```

### Development with Docker

```bash
# Start all services
docker-compose up -d

# View logs
docker-compose logs -f api

# Run tests
docker-compose exec api pytest
```

### Production Docker Setup

#### Prerequisites
- Docker Desktop / Docker Engine 24+
- Docker Compose v2+
- Kite Connect API credentials (API Key, Secret, User ID, Password)
- Kite 2FA enabled with TOTP secret (for auto-login)

#### 1. Build and Start Services
```bash
# Build images and start all services
docker compose up --build -d

# Verify all containers are healthy
docker compose ps
```

**Docker Optimizations Applied:**
- **`.dockerignore`** excludes 3.6 GB of data/caches → build context ~1 MB
- **Multi-stage Dockerfile** (builder → playwright → runtime)
- **Final image ~2-3 GB** (no build tools, dev deps, pip cache, or data)

#### 2. Kite Authentication (Required for Data Ingestion)

**Option A: Manual Login (Most Reliable)**
```bash
# Run manual login in container
docker compose run --rm ingest python manual_login.py
```

**Option B: Automated TOTP Login (May Require Non-Headless Mode)**
```bash
# Run auto-login - uses Playwright + TOTP from .env
docker compose run --rm ingest python -m trading_agent.ingest.kite_auto_login
```
Note: Auto-login works best in non-headless mode. If it fails, use manual login.

The access token persists in the mounted `./data/` volume and is reused by all services.

#### 3. Backfill Historical Data
```bash
# Full backfill (all symbols in watchlist)
docker compose run --rm ingest trading-agent ingest --all

# Specific symbols and date range
docker compose run --rm ingest trading-agent ingest --symbol RELIANCE TCS INFY --days 100

# Verify data loaded
docker compose exec api trading-agent storage-stats
```

#### 4. Access Services
| Service | URL | Description |
|---------|-----|-------------|
| API | http://localhost:8000 | REST API + WebSocket |
| API Docs | http://localhost:8000/docs | Swagger UI |
| Frontend | http://localhost:5173 | React dashboard (if enabled) |
| PostgreSQL | localhost:5433 | `trading`/`trading`/`trading` |
| Redis | localhost:6379 | Caching & pub/sub |

#### 5. Common Operations
```bash
# View service logs
docker compose logs -f api        # API server
docker compose logs -f ingest     # Data ingestion
docker compose logs -f scheduler  # Periodic tasks

# Run CLI commands in container
docker compose exec api trading-agent --help
docker compose exec api trading-agent diagnose RELIANCE
docker compose exec api trading-agent backtest RELIANCE --start 2024-01-01

# Run tests in container
docker compose exec api pytest tests/unit/ -v

# Stop all services
docker compose down

# Stop and remove volumes (clean slate)
docker compose down -v

# Rebuild after code changes
docker compose up --build -d
```

#### Service Architecture
- **api**: FastAPI server (port 8000), auto-restart
- **ingest**: One-time historical fetch (`restart: "no"`)
- **scheduler**: Periodic ingestion + maintenance (cron), auto-restart
- **db**: PostgreSQL 16 with migrations auto-applied
- **redis**: Redis 7 for caching
- **frontend**: Vite dev server (optional)

#### Data Persistence
- `./data/` → `/app/data` (Parquet files, access_token.txt, SQLite fallback)
- `./models/` → `/app/models` (ML model artifacts)
- `postgres_data` volume → `/var/lib/postgresql/data` (PostgreSQL)
- `redis_data` volume → `/data` (Redis)

#### Troubleshooting

**Auto-login fails:**
```bash
# Check TOTP secret is valid (32-char base32)
docker compose run --rm ingest python -c "import pyotp; import os; print(pyotp.TOTP(os.getenv('KITE_TOTP_SECRET')).now())"

# Run with debug
docker compose run --rm ingest python -m trading_agent.ingest.kite_auto_login 2>&1 | head -50
```

**Database connection issues:**
```bash
# Check DB health
docker compose exec db pg_isready -U trading -d trading

# View DB logs
docker compose logs db
```

**Port conflicts:**
```bash
# Change ports in docker-compose.yml if 8000/5433/6379/5173 are in use
```

## CLI Usage

The package installs a `trading-agent` command with subcommands:

```bash
# Authenticate with Kite
trading-agent login

# Fetch historical data
trading-agent ingest RELIANCE TCS INFY --interval 5minute --days 100

# Backfill years of data
trading-agent backfill --years 2

# Run backtest
trading-agent backtest RELIANCE TCS --start 2024-01-01 --end 2024-06-30

# Diagnose signal edge
trading-agent diagnose RELIANCE TCS

# Run walk-forward optimization
trading-agent optimize walk-forward RELIANCE TCS

# Run walk-forward optimization on all symbols
trading-agent optimize walk-forward

# Train ML model (v1)
trading-agent optimize ml-model RELIANCE TCS

# Train enhanced ML model (v2) with regime features
trading-agent optimize ml-model-v2 RELIANCE TCS

# Run strategy lab grid search
trading-agent optimize strategy-lab RELIANCE

# Run trading pipeline
trading-agent pipeline

# Start API server
trading-agent api

# Fetch latest news
trading-agent news

# Show storage statistics
trading-agent storage-stats

# Compact Parquet files
trading-agent compact RELIANCE
```

### CLI Commands Reference

| Command | Description |
|---------|-------------|
| `ingest` | Fetch historical candle data |
| `backfill` | Backfill years of historical data |
| `login` | Authenticate with Kite Connect |
| `backtest` | Run vectorized backtest |
| `diagnose` | Diagnose signal edge vs random baseline |
| `news` | Fetch latest financial news |
| `api` | Run FastAPI server |
| `pipeline` | Run trading pipeline once |
| `storage-stats` | Show Parquet storage statistics |
| `compact` | Compact Parquet files for symbol |
| `optimize walk-forward` | Walk-forward parameter optimization |
| `optimize ml-model` | Train standard ML models |
| `optimize ml-model-v2` | Train enhanced ML (regime features) |
| `optimize strategy-lab` | Strategy grid search |

## Project Structure Details

### Core Modules

| Module | Purpose |
|--------|---------|
| `config.py` | Pydantic Settings for all configuration |
| `quant_indicators.py` | Vectorized technical indicators (RSI, ADX, ATR, MACD, etc.) |
| `pattern_detection.py` | Candlestick and chart pattern detection |
| `signals.py` | Rule-based signal evaluation |
| `position_sizing.py` | ATR-based position sizing with Kelly/Optimal f |
| `circuit_breaker.py` | Resilience pattern for external APIs |
| `freshness.py` | Data freshness monitoring |
| `ml_versioning.py` | ML model registry with versioning |

### Ingestion Modules

| Module | Purpose |
|--------|---------|
| `kite_auth.py` | Kite Connect authentication |
| `instrument_lookup.py` | NSE symbol ↔ token mapping |
| `fetch_historical.py` | Historical data with rate limiting |
| `live_ticker.py` | WebSocket live data + candle aggregation |
| `gap_healer.py` | Missing candle detection and repair |
| `watchlist_resolved.py` | Watchlist management |
| `kite_auto_login.py` | Automated TOTP login with Playwright |
| `fetch_news.py` | Multi-source news fetching |

### Strategy Modules

| Module | Purpose |
|--------|---------|
| `candidates.py` | Trading candidate generation |
| `decision_agent.py` | Rule-based decisions |
| `llm_decision_agent.py` | LLM-based decisions |
| `ml_decision_agent.py` | ML model inference |
| `ml_features.py` | Feature engineering (v1/v2) |
| `train_ml_model.py` | Standard ML training |
| `train_ml_model_v2.py` | Enhanced training with regimes |
| `session_features.py` | Intraday session features |
| `hybrid_strategy.py` | Multi-agent combination |
| `fast_hybrid_strategy.py` | Real-time optimized hybrid |
| `shadow.py` | Shadow testing framework |

### Risk Modules

| Module | Purpose |
|--------|---------|
| `portfolio_risk.py` | Portfolio-level risk limits |
| `position_monitor.py` | Real-time position management |
| `wallet.py` | Paper trading with realistic charges |

### Analysis Modules

| Module | Purpose |
|--------|---------|
| `opportunity_finder.py` | Signal combination and ranking |
| `rankings.py` | Momentum, mean reversion, volume rankings |
| `technical_summary.py` | Automated technical analysis |
| `sentiment.py` | News sentiment analysis |
| `digest.py` | Daily trading digests |

## API Endpoints

```
GET  /api/v1/health              # Health check
GET  /api/v1/symbols             # List symbols
GET  /api/v1/symbols/{symbol}    # Symbol details
GET  /api/v1/history/{symbol}    # Historical candles
GET  /api/v1/opportunities       # Current opportunities
GET  /api/v1/positions           # Open positions
POST /api/v1/positions           # Create position
POST /api/v1/positions/{id}/close # Close position
GET  /api/v1/wallet/summary      # Wallet snapshot
GET  /api/v1/wallet/metrics      # Performance metrics
GET  /api/v1/watchlist           # Watchlist
POST /api/v1/watchlist           # Add to watchlist
GET  /api/v1/liveness/freshness  # Data freshness
GET  /api/v1/liveness/storage    # Storage stats
```

## Testing

```bash
# Run all tests (154 passing)
pytest

# Run with coverage
pytest --cov=trading_agent --cov-report=html

# Run specific module
pytest tests/unit/test_core.py -v

# Run all unit tests
pytest tests/unit/ -v

# Run integration tests
pytest tests/test_portfolio_exposure.py tests/test_position_monitor.py tests/test_opportunity_exclusions.py -v
```

## Configuration

All configuration via `.env` file or environment variables:

```env
# Required for Kite Connect
KITE_API_KEY=your_key
KITE_API_SECRET=your_secret
KITE_TOTP_SECRET=your_totp  # For auto-login

# Optional
DB_URL=postgresql://user:pass@localhost/trading
API_PORT=8000
LOG_LEVEL=DEBUG
```

## Research Pipeline

The research module provides:

1. **Edge Diagnosis** (`diagnose_edge.py`): Measures signal edge vs random baseline
2. **Strategy Lab** (`strategy_lab.py`): Rapid parameter grid/random search
3. **Walk-Forward Optimizer** (`walk_forward_optimizer.py`): Proper out-of-sample validation
4. **Backtesting** (`backtest.py`): Vectorized simulation with realistic charges

## ML Pipeline

Two training approaches:

- **v1** (`train_ml_model.py`): Standard LightGBM with time-series CV
- **v2** (`train_ml_model_v2.py`): Enhanced with regime features, cost-aware labeling, per-symbol thresholds

## Frontend

React + TypeScript + Vite dashboard in `frontend/`:

```bash
cd frontend
npm install
npm run dev     # Development
npm run build   # Production build
```

## Documentation

| File | Description |
|------|-------------|
| [USER_GUIDE.md](USER_GUIDE.md) | **Complete User Guide** — installation, daily workflow, CLI reference, Docker, troubleshooting |
| [PRD.md](PRD.md) | Product Requirements Document — vision, success metrics, features, risks, timeline |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Architecture Decision Records (10 ADRs) — binding architectural decisions |
| [DESIGN.md](DESIGN.md) | System design — module responsibilities, data flow, schema, deployment |
| [RULES.md](RULES.md) | Code style, git, architecture rules, data/trading/ingestion/testing/Docker/security rules |
| [EDGE_ANALYSIS.md](EDGE_ANALYSIS.md) | Signal edge measurement methodology and results |
| [STRATEGIES.md](STRATEGIES.md) | Strategy candidates, promotion rules, and measured results |
| [ML_REDESIGN_SUMMARY.md](ML_REDESIGN_SUMMARY.md) | Enhanced ML v2 walk-forward validation summary |
| [TASKS.md](TASKS.md) | Task tracker — current sprint, backlog, issues, release checklist |
| [MEMORY.md](MEMORY.md) | Decision log, technical learnings, metrics, debugging patterns |

## License

MIT License - see LICENSE file for details.

## Disclaimer

**Paper trading only.** This software is for research and educational purposes. Nothing in this repository places real orders. The `positions` table is a manual ledger. Past performance does not guarantee future results. Trade at your own risk.