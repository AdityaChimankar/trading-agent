"""Canonical filesystem locations.

Every module that reads or writes a file resolves it through here instead of
hardcoding a relative path. Files used to be opened by bare name (`.access_token`,
`symbols.txt`, ...), which silently resolved against the *current working
directory* - so a command run from anywhere but the project root either failed
or, worse, wrote a fresh file somewhere unexpected. Deriving everything from
`__file__` makes the location independent of both the CWD and where a module
sits in the package tree.
"""
from pathlib import Path

# <root>/paths.py -> the directory containing this file IS the project root.
PROJECT_ROOT = Path(__file__).resolve().parent

# --- storage -------------------------------------------------------------
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "trading_agent.db"
SCHEMA_PATH = PROJECT_ROOT / "storage" / "schema.sql"

# --- parquet storage -----------------------------------------------------
PARQUET_DIR = DATA_DIR / "parquet"
CANDLES_PARQUET_DIR = PARQUET_DIR / "candles"

# --- credentials & market-data inputs ------------------------------------
ACCESS_TOKEN_PATH = PROJECT_ROOT / ".access_token"
SYMBOLS_TXT = PROJECT_ROOT / "symbols.txt"
INSTRUMENTS_CSV = PROJECT_ROOT / "ingest" / "instruments.csv"
WATCHLIST_RESOLVED = PROJECT_ROOT / "ingest" / "watchlist_resolved.py"

# --- model artifacts -----------------------------------------------------
MODEL_DIR = PROJECT_ROOT / "models"
ML_MODEL_PATH = MODEL_DIR / "ml_model.joblib"
