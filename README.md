# Trading Agent - NSE Intraday Trading Signal System

A local-first research pipeline for NSE intraday signals. It ingests live and historical candles, computes deterministic technical/pattern features, and runs **three independent decision paths side by side** — rule-based, LLM, and ML — so their calls can be compared on real outcomes instead of argued about.

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
├── tests/              # Unit and integration tests
├── frontend/           # React + Vite dashboard
└── docker-compose.yml  # Local development stack
```

## Key Features

- **Multi-path Decision Making**: Rule-based, LLM, and ML signals run independently
- **Walk-Forward Optimization**: Proper out-of-sample parameter validation
- **Vectorized Backtesting**: Fast, NumPy-based simulation engine
- **Gap Healing**: Automatic detection and repair of missing candles
- **Paper Trading**: Full position management with realistic charges
- **Modern Stack**: Python 3.11+, FastAPI, SQLAlchemy 2.0, Polars, LightGBM
- **Type Safety**: Full type hints with mypy, ruff, black

## Quick Start

### Prerequisites

- Python 3.11+
- PostgreSQL 16+ (or SQLite for development)
- Kite Connect API credentials
- Node.js 20+ (for frontend)

### Installation

```bash
# Clone repository
git clone <repo-url>
cd trading-agent

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Install dependencies
pip install -e ".[dev]"

# Install Playwright for auto-login
playwright install chromium

# Copy and configure environment
cp .env.example .env
# Edit .env with your Kite credentials

# Database is initialized automatically on API startup
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

## CLI Usage

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

# Train ML model
trading-agent optimize ml-model RELIANCE TCS

# Train enhanced ML model (v2)
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
# Run all tests
pytest

# Run with coverage
pytest --cov=trading_agent --cov-report=html

# Run specific module
pytest tests/unit/test_core.py -v

# Run all unit tests (61 tests passing)
pytest tests/unit/ -v

# Run integration tests
pytest tests/integration/ -v
```

All 61 unit tests pass across 4 test files:
- `test_core.py`: 29 tests (indicators, patterns, signals, position sizing, circuit breaker, freshness)
- `test_strategy.py`: 10 tests (candidates, rule agent, ML features, hybrid strategy)
- `test_risk.py`: 16 tests (portfolio risk, position monitor, paper wallet)
- `test_analysis.py`: 6 tests (opportunity finder, rankings)

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

## Contributing

1. Fork the repository
2. Create feature branch (`git checkout -b feature/amazing-feature`)
3. Run tests and linting (`pytest && ruff check && black --check`)
4. Commit changes (`git commit -m 'Add amazing feature'`)
5. Push to branch (`git push origin feature/amazing-feature`)
6. Open Pull Request

## License

MIT License - see LICENSE file for details.

## Disclaimer

**Paper trading only.** This software is for research and educational purposes. Nothing in this repository places real orders. The `positions` table is a manual ledger. Past performance does not guarantee future results. Trade at your own risk.