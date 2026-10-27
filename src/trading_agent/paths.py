"""Canonical filesystem locations.

Every module that reads or writes a file resolves it through here instead of
hardcoding a relative path. Files used to be opened by bare name (`.access_token`,
`symbols.txt`, ...), which silently resolved against the *current working
directory* - so a command run from anywhere but the project root either failed
or, worse, wrote a fresh file somewhere unexpected. Source checkouts resolve to
the repository root; installed deployments may set ``TRADING_AGENT_HOME``.
"""
import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = PACKAGE_ROOT.parent.parent
PROJECT_ROOT = Path(
    os.getenv(
        "TRADING_AGENT_HOME",
        SOURCE_ROOT if (SOURCE_ROOT / "pyproject.toml").exists() else Path.cwd(),
    )
).expanduser().resolve()

# --- storage -------------------------------------------------------------
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "trading_agent.db"
SCHEMA_PATH = PACKAGE_ROOT / "storage" / "schema.sql"

# --- parquet storage -----------------------------------------------------
PARQUET_DIR = DATA_DIR / "parquet"
CANDLES_PARQUET_DIR = PARQUET_DIR / "candles"

# --- credentials & market-data inputs ------------------------------------
ACCESS_TOKEN_PATH = DATA_DIR / ".access_token"
SYMBOLS_TXT = PROJECT_ROOT / "symbols.txt"
LIVE_SYMBOLS_TXT = PROJECT_ROOT / "live_symbols.txt"
INSTRUMENTS_CSV = PACKAGE_ROOT / "ingest" / "instruments.csv"
WATCHLIST_RESOLVED = PACKAGE_ROOT / "ingest" / "watchlist_resolved.py"

# --- model artifacts -----------------------------------------------------
MODEL_DIR = PROJECT_ROOT / "models"
ML_MODEL_PATH = MODEL_DIR / "ml_model.joblib"
