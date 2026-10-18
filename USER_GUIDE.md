# Trading Agent - Complete User Guide

## Overview

Trading Agent is a local-first research pipeline for NSE intraday trading signals. It ingests live and historical candle data from Kite Connect, computes deterministic technical indicators and patterns, and runs three independent decision paths (rule-based, LLM, and ML) for comparison.

**Key Features:**
- Multi-path decision making (Rule, LLM, ML)
- Walk-forward optimization
- Vectorized backtesting
- Gap healing for missing candles
- Paper trading with realistic charges
- Modern stack: Python 3.11+, FastAPI, SQLAlchemy 2.0, Polars, LightGBM

---

## Prerequisites

| Requirement | Version | Notes |
|-------------|---------|-------|
| Python | 3.11+ | Required |
| Git | Any | For cloning |
| Kite Connect API | Paid | ₹2000/mo base + ₹500/mo historical |
| Docker | 24+ | Optional, for containerized deployment |
| Node.js | 20+ | Optional, for frontend |

---

## Installation

### 1. Clone Repository

```bash
git clone https://github.com/your-org/trading-agent.git
cd trading-agent
```

### 2. Create Virtual Environment

```bash
# Windows
python -m venv .venv
.venv\Scripts\activate

# Linux/Mac
python -m venv .venv
source .venv/bin/activate
```

### 3. Install Dependencies

```bash
# Install package in editable mode with dev dependencies
pip install -e ".[dev]"

# Install Playwright for auto-login (optional)
playwright install chromium
```

### 4. Configure Environment

```bash
# Copy example environment file
cp .env.example .env
```

Edit `.env` with your credentials:

```env
# REQUIRED - Get from https://developers.kite.trade
KITE_API_KEY=your_api_key
KITE_API_SECRET=your_api_secret
KITE_TOTP_SECRET=your_32_char_base32_secret  # From 2FA QR code
KITE_USER_ID=your_user_id
KITE_PASSWORD=your_password
KITE_REDIRECT_URL=http://localhost

# OPTIONAL - For LLM/sentiment analysis
OPENROUTER_API_KEY=your_openrouter_key

# OPTIONAL - Database (default: SQLite)
DB_URL=postgresql://user:pass@localhost/trading

# OPTIONAL - API Server
API_PORT=8000
LOG_LEVEL=INFO
```

**Getting Kite TOTP Secret:**
1. Enable 2FA on Kite web/app
2. Scan QR code with authenticator app
3. Use "Enter key manually" option to see the 32-character base32 secret
4. Paste that into `KITE_TOTP_SECRET`

---

## Daily Workflow

### 1. Generate Access Token (Required Daily)

The Kite access token expires daily at ~7:30 AM IST. You must generate a new one each trading day.

**Option A: Manual Login (Recommended)**
```bash
python manual_login.py
```
This opens the login URL, you log in via browser, then paste the `request_token` back.

**Option B: CLI Command**
```bash
trading-agent login
```

**Option C: Docker**
```bash
docker compose run --rm ingest python manual_login.py
```

### 2. Fetch Historical Data

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

### 3. Run Analysis & Backtests

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

### 4. Live Trading Pipeline (Market Hours: 9:15-15:30 IST)

Run these in separate terminals:

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

---

## CLI Command Reference

### Authentication
| Command | Description |
|---------|-------------|
| `trading-agent login` | Authenticate with Kite Connect |

### Data Ingestion
| Command | Description |
|---------|-------------|
| `trading-agent ingest [SYMBOLS...]` | Fetch historical data |
| `trading-agent backfill [SYMBOLS...]` | Backfill years of data |
| `trading-agent news [SYMBOLS...]` | Fetch latest financial news |

### Analysis & Backtesting
| Command | Description |
|---------|-------------|
| `trading-agent backtest SYMBOLS...` | Run vectorized backtest |
| `trading-agent diagnose SYMBOLS...` | Diagnose signal edge |
| `trading-agent pipeline [SYMBOLS...]` | Run trading pipeline once |

### Optimization
| Command | Description |
|---------|-------------|
| `trading-agent optimize walk-forward [SYMBOLS...]` | Walk-forward optimization |
| `trading-agent optimize ml-model [SYMBOLS...]` | Train standard ML model |
| `trading-agent optimize ml-model-v2 [SYMBOLS...]` | Train enhanced ML model |
| `trading-agent optimize strategy-lab SYMBOL` | Strategy grid search |

### Server & Utilities
| Command | Description |
|---------|-------------|
| `trading-agent api` | Run FastAPI server |
| `trading-agent storage-stats` | Show storage statistics |
| `trading-agent compact SYMBOL` | Compact Parquet files |

---

## Docker Deployment

### Quick Start

```bash
# Build and start all services
docker compose up --build -d

# Check status
docker compose ps
```

### Daily Login in Container

```bash
docker compose run --rm ingest python manual_login.py
```

### Fetch Data in Container

```bash
# Full backfill
docker compose run --rm ingest trading-agent ingest --all

# Specific symbols
docker compose run --rm ingest trading-agent ingest --symbol RELIANCE TCS INFY --days 100

# Verify data
docker compose exec api trading-agent storage-stats
```

### Service Access

| Service | URL | Description |
|---------|-----|-------------|
| API | http://localhost:8000 | REST API + WebSocket |
| API Docs | http://localhost:8000/docs | Swagger UI |
| Frontend | http://localhost:5173 | React dashboard |
| PostgreSQL | localhost:5433 | `trading`/`trading`/`trading` |
| Redis | localhost:6379 | Caching & pub/sub |

### Common Operations

```bash
# View logs
docker compose logs -f api        # API server
docker compose logs -f ingest     # Data ingestion
docker compose logs -f scheduler  # Periodic tasks

# Run CLI in container
docker compose exec api trading-agent --help
docker compose exec api trading-agent diagnose RELIANCE

# Run tests
docker compose exec api pytest tests/unit/ -v

# Stop services
docker compose down

# Clean slate (removes volumes)
docker compose down -v

# Rebuild after changes
docker compose up --build -d
```

---

## Architecture Overview

```
trading-agent/
├── src/trading_agent/
│   ├── core/           # Config, indicators, signals, logging
│   ├── ingest/         # Kite Connect, historical, live, news
│   ├── storage/        # SQLite + Parquet storage
│   ├── strategy/       # Signals, candidates, decision agents
│   ├── risk/           # Portfolio risk, position monitoring
│   ├── analysis/       # Opportunities, rankings, digests
│   ├── api/            # FastAPI REST + WebSocket
│   ├── research/       # Backtesting, edge diagnosis
│   ├── optimize/       # Walk-forward optimization
│   └── cli/            # Main CLI entry point
├── tests/              # Unit & integration tests
├── frontend/           # React + Vite dashboard
└── docker-compose.yml  # Local development stack
```

---

## Data Flow

1. **Ingestion**: Kite Connect REST/WebSocket → Parquet files (partitioned by symbol/interval)
2. **Processing**: Technical indicators (RSI, ADX, ATR, MACD) + Pattern detection
3. **Signals**: Rule-based, ML, LLM decision agents run independently
4. **Risk**: Position sizing, portfolio limits, stop/target monitoring
5. **Analysis**: Opportunity ranking, sentiment, daily digests
6. **API**: REST endpoints serve data to frontend

---

## Configuration

All configuration via `.env` file or environment variables:

```env
# Kite Connect (required)
KITE_API_KEY=
KITE_API_SECRET=
KITE_TOTP_SECRET=
KITE_USER_ID=
KITE_PASSWORD=
KITE_REDIRECT_URL=http://localhost

# Database
DB_URL=sqlite:///data/trading.db

# API
API_HOST=0.0.0.0
API_PORT=8000
API_RELOAD=false

# ML
ML_MODEL_DIR=models
ML_FEATURE_VERSION=v2

# Logging
LOG_LEVEL=INFO
LOG_FORMAT=console
LOG_FILE=logs/trading.log
```

---

## Testing

```bash
# Run all tests
pytest

# Run unit tests only
pytest tests/unit/ -v

# Run with coverage
pytest --cov=trading_agent --cov-report=html

# Run specific module
pytest tests/test_signals.py -v
```

---

## Troubleshooting

### Common Issues

| Issue | Solution |
|-------|----------|
| `Invalid api_key or access_token` | Run `manual_login.py` to generate fresh token |
| `No data for symbol` | Run `trading-agent ingest SYMBOL` first |
| `Module not found` errors | `pip install -e ".[dev]"` in project root |
| Playwright auto-login fails | Use `manual_login.py` instead |
| Database locked | Only one process should write to SQLite |
| `Module trading_agent not found` | Run `pip install -e .` from project root |

### Auto-login Issues

```bash
# Check TOTP secret is valid
python -c "import pyotp, os; print(pyotp.TOTP(os.getenv('KITE_TOTP_SECRET')).now())"

# Run with debug
python -m trading_agent.ingest.kite_auto_login 2>&1 | head -50
```

### Database Issues

```bash
# Check DB health
docker compose exec db pg_isready -U trading -d trading

# View DB logs
docker compose logs db
```

### Port Conflicts

Change ports in `docker-compose.yml` if 8000/5433/6379/5173 are in use.

---

## Development

### Code Quality

```bash
# Format code
black src/ tests/

# Lint
ruff check src/ tests/

# Type check
mypy src/trading_agent
```

### Pre-commit Hooks

```bash
pre-commit install
pre-commit run --all-files
```

---

## Project Structure Details

### Core Modules

| Module | Purpose |
|--------|---------|
| `config.py` | Pydantic Settings for all configuration |
| `quant_indicators.py` | Vectorized RSI, ATR, ADX, MACD, etc. |
| `pattern_detection.py` | Candlestick and chart patterns |
| `signals.py` | Rule-based signal evaluation |
| `position_sizing.py` | ATR-based sizing with Kelly/Optimal f |
| `circuit_breaker.py` | Resilience for external APIs |
| `freshness.py` | Data freshness monitoring |

### Ingestion Modules

| Module | Purpose |
|--------|---------|
| `kite_auth.py` | Manual Kite login flow |
| `kite_auto_login.py` | Automated TOTP login (Playwright) |
| `fetch_historical.py` | Resumable backfill, rate-limited |
| `live_ticker.py` | WebSocket → 1-min candles |
| `gap_healer.py` | Missing candle detection/repair |
| `fetch_news.py` | Multi-source news fetching |

### Strategy Modules

| Module | Purpose |
|--------|---------|
| `decision_agent.py` | Rule-based BUY/SELL/HOLD |
| `ml_decision_agent.py` | ML v1 inference |
| `ml_decision_agent_v2.py` | ML v2 (walk-forward, regimes) |
| `llm_decision_agent.py` | LLM decisions via OpenRouter |
| `candidates.py` | 11 candidate strategies |
| `hybrid_strategy.py` | Backtest-selects best mode |

### Risk Modules

| Module | Purpose |
|--------|---------|
| `portfolio_risk.py` | Risk budget, correlation clusters |
| `position_monitor.py` | Live P&L, stop/target checks |
| `wallet.py` | Paper trading accounting |

---

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

---

## Disclaimer

**Paper trading only.** This software is for research and educational purposes. Nothing in this repository places real orders. The `positions` table is a manual ledger. Past performance does not guarantee future results. Trade at your own risk.

---

## Support

- Run any command with `--help` for options
- Check logs in `./logs/` directory
- Run self-tests: `python -m trading_agent.research.backtest --selftest`
- View API docs at `http://localhost:8000/docs`