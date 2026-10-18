#!/usr/bin/env python
"""
Quick Start Guide - Trading Agent

This guide walks you through setting up and running the Trading Agent step by step.
"""

# =============================================================================
# STEP 1: PREREQUISITES
# =============================================================================
"""
REQUIRED:
- Python 3.11 or higher
- Git
- Kite Connect API subscription (₹2000/month for historical + live data)

OPTIONAL:
- Docker & Docker Compose (for containerized deployment)
- Node.js 20+ (for frontend dashboard)
- PostgreSQL 16+ (if not using SQLite)

VERIFY:
    python --version  # Should be 3.11+
    git --version
"""

# =============================================================================
# STEP 2: CLONE & SETUP
# =============================================================================
"""
# Clone the repository
git clone <your-repo-url>
cd trading-agent

# Create virtual environment
python -m venv .venv

# Activate virtual environment
# Windows:
.venv\Scripts\activate
# Linux/Mac:
source .venv/bin/activate

# Install package in editable mode with dev dependencies
pip install -e ".[dev]"

# Install Playwright browsers (for auto-login)
playwright install chromium
"""

# =============================================================================
# STEP 3: CONFIGURE ENVIRONMENT
# =============================================================================
"""
# Copy environment template
cp .env.example .env

# Edit .env with your credentials
# REQUIRED:
# KITE_API_KEY=your_api_key_from_kite
# KITE_API_SECRET=your_api_secret_from_kite
# KITE_TOTP_SECRET=your_32_char_base32_totp_secret
# KITE_USER_ID=your_kite_user_id
# KITE_PASSWORD=your_kite_password
# KITE_REDIRECT_URL=http://localhost

# OPTIONAL:
# OPENROUTER_API_KEY=your_openrouter_key  # For LLM/sentiment
# DB_URL=postgresql://user:pass@localhost/trading  # Default: SQLite
# API_PORT=8000
# LOG_LEVEL=INFO

# GET KITE CREDENTIALS:
# 1. Go to https://developers.kite.trade
# 2. Create an app → Get API Key & Secret
# 3. Subscribe to "Historical Data" addon (₹500/month)
# 4. Enable 2FA on Kite → Get TOTP secret from QR code
#    (Use authenticator app to scan QR, then use "Enter key manually" to see base32 secret)
"""

# =============================================================================
# STEP 4: MANUAL LOGIN (REQUIRED DAILY)
# =============================================================================
"""
The access token expires daily at ~7:30 AM IST. You must generate a new one each trading day.

# Option A: Use the helper script (RECOMMENDED)
python manual_login.py

# This will:
# 1. Print the login URL
# 2. Open it in browser
# 3. You log in with User ID, Password, TOTP
# 4. Copy request_token from redirect URL
# 4. Paste it back in terminal
# 5. Saves .access_token file

# Option B: Manual via browser
# 1. Run: python -m trading_agent.ingest.kite_auth
# 2. Copy the printed login URL
# 3. Open in browser, log in
# 4. Copy request_token from redirect
# 5. Generate session programmatically
"""

# =============================================================================
# STEP 5: FETCH HISTORICAL DATA
# =============================================================================
"""
# Fetch data for specific symbols (100 days)
trading-agent ingest RELIANCE TCS INFY --days 100

# Fetch for entire watchlist (default 100 days)
trading-agent ingest

# Backfill 2 years of data
trading-agent backfill --years 2

# Check what data was stored
trading-agent storage-stats
"""

# =============================================================================
# STEP 6: RUN ANALYSIS & BACKTESTS
# =============================================================================
"""
# Diagnose signal edge (compares strategy vs random baseline)
trading-agent diagnose RELIANCE TCS

# Run vectorized backtest
trading-agent backtest RELIANCE TCS --start 2024-01-01 --end 2024-06-30

# Run walk-forward optimization
trading-agent optimize walk-forward RELIANCE TCS

# Train ML model (enhanced v2)
trading-agent optimize ml-model-v2 RELIANCE TCS

# Strategy lab (grid search)
trading-agent optimize strategy-lab RELIANCE
"""

# =============================================================================
# STEP 7: RUN LIVE TRADING PIPELINE
# =============================================================================
"""
# Prerequisites:
# 1. Valid access token (run manual_login.py first)
# 2. Historical data fetched

# Run trading pipeline once (generates signals, opportunities)
trading-agent pipeline

# Start API server (for dashboard)
trading-agent api

# In another terminal, start live ticker (run during market hours 9:15-15:30)
python -m trading_agent.ingest.live_ticker

# Run scheduler (background tasks: news, decisions, position monitoring)
python -m trading_agent.scripts.scheduler
"""

# =============================================================================
# STEP 8: DOCKER DEPLOYMENT (OPTIONAL)
# =============================================================================
"""
# Build and start all services
docker compose up --build -d

# View logs
docker compose logs -f api

# Run manual login in container
docker compose run --rm ingest python manual_login.py

# Fetch data in container
docker compose run --rm ingest trading-agent ingest

# Access services:
# - API: http://localhost:8000
# - API Docs: http://localhost:8000/docs
# - Frontend: http://localhost:5173
# - PostgreSQL: localhost:5433
"""

# =============================================================================
# TROUBLESHOOTING
# =============================================================================
"""
COMMON ISSUES:

1. "Invalid api_key or access_token"
   → Run manual_login.py to generate fresh token
   → Token expires daily at ~7:30 AM IST

2. "No data for symbol"
   → Run trading-agent ingest SYMBOL first
   → Check symbol is in watchlist (trading-agent storage-stats)

3. "Module not found" errors
   → pip install -e ".[dev]" in project root
   → Ensure virtual environment is activated

4. Playwright auto-login fails
   → Use manual_login.py instead
   → Or run: playwright install chromium

5. Database locked errors
   → Only one process should write to SQLite at a time
   → Use PostgreSQL for production

6. "Module trading_agent not found"
   → Run: pip install -e . from project root

GETTING HELP:
- Run any command with --help for options
- Check logs in ./logs/ directory
- Run self-tests: python -m trading_agent.research.backtest --selftest
"""

if __name__ == "__main__":
    print(__doc__)