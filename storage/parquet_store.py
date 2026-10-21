"""
Parquet storage for historical candles - 10-100x faster reads than SQLite.
Uses partitioned layout: data/parquet/candles/symbol=RELIANCE/interval=minute/part-*.parquet
"""
import os
from pathlib import Path
from datetime import datetime
from typing import Optional, List
import pandas as pd

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
    PARQUET_AVAILABLE = True
except ImportError:
    PARQUET_AVAILABLE = False

try:
    import fastparquet
    FASTPARQUET_AVAILABLE = True
except ImportError:
    FASTPARQUET_AVAILABLE = False

from paths import CANDLES_PARQUET_DIR, DB_PATH
from core.freshness import candle_timestamp, MINUTE_KEY_FMT


def _check_parquet():
    if not PARQUET_AVAILABLE and not FASTPARQUET_AVAILABLE:
        raise RuntimeError("Parquet support not installed. Run: pip install pyarrow")


def get_parquet_engine() -> str:
    """Return preferred parquet engine."""
    if PARQUET_AVAILABLE:
        return "pyarrow"
    elif FASTPARQUET_AVAILABLE:
        return "fastparquet"
    return None


def symbol_parquet_dir(symbol: str, interval: str = "minute") -> Path:
    """Get parquet directory for a symbol/interval."""
    return CANDLES_PARQUET_DIR / f"symbol={symbol}" / f"interval={interval}"


def write_candles_parquet(symbol: str, df: pd.DataFrame, interval: str = "minute") -> Path:
    """
    Write candles DataFrame to parquet - single file per symbol/interval for fast reads.
    """
    _check_parquet()
    
    if df.empty:
        return None
    
    # Ensure timestamp is string in correct format
    df = df.copy()
    df["timestamp"] = df["timestamp"].apply(candle_timestamp)
    
    # Add metadata columns
    df["symbol"] = symbol
    df["interval"] = interval
    df["ingested_at"] = datetime.now().isoformat()
    
    # Sort by timestamp
    df = df.sort_values("timestamp").reset_index(drop=True)
    
    # Write single file per symbol/interval (not partitioned)
    out_dir = symbol_parquet_dir(symbol, interval)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "data.parquet"
    
    engine = get_parquet_engine()
    if engine == "pyarrow":
        pq.write_table(
            pa.Table.from_pandas(df),
            str(out_file),
            compression="snappy",
        )
    else:
        import fastparquet as fp
        fp.write(str(out_file), df, compression="snappy", append=False)
    
    return out_dir


def read_candles_parquet(
    symbol: str,
    interval: str = "minute",
    start_date: str = None,
    end_date: str = None,
    limit: int = None
) -> pd.DataFrame:
    """
    Read candles from parquet with optional date filtering and limit.
    Returns DataFrame with columns: timestamp, open, high, low, close, volume
    """
    _check_parquet()
    
    parquet_dir = symbol_parquet_dir(symbol, interval)
    parquet_file = parquet_dir / "data.parquet"
    
    if not parquet_file.exists():
        return pd.DataFrame()
    
    df = pd.read_parquet(str(parquet_file))
    
    if df.empty:
        return pd.DataFrame()
    
    # Filter by date if specified
    if start_date:
        df = df[df["timestamp"] >= start_date]
    if end_date:
        df = df[df["timestamp"] <= end_date]
    
    # Sort and limit
    df = df.sort_values("timestamp").reset_index(drop=True)
    if limit:
        df = df.tail(limit)
    
    # Return only core columns
    return df[["timestamp", "open", "high", "low", "close", "volume"]]


def load_candles_parquet(
    symbol: str,
    limit: int = 200,
    interval: str = "minute"
) -> pd.DataFrame:
    """
    Drop-in replacement for quant_indicators.load_candles() using parquet.
    Returns chronological DataFrame (oldest first).
    """
    df = read_candles_parquet(symbol, interval=interval, limit=limit)
    return df


def migrate_sqlite_to_parquet(batch_size: int = 100) -> dict:
    """
    Migrate all candles from SQLite to Parquet.
    Returns stats: {symbols_migrated, total_rows, errors}
    """
    import sqlite3
    
    _check_parquet()
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    # Get unique symbols from candles table
    symbols = [r["symbol"] for r in conn.execute("SELECT DISTINCT symbol FROM candles").fetchall()]
    
    stats = {"symbols_migrated": 0, "total_rows": 0, "errors": []}
    
    for symbol in symbols:
        try:
            # Read all candles for this symbol in batches
            offset = 0
            all_dfs = []
            
            while True:
                df = pd.read_sql_query(
                    "SELECT * FROM candles WHERE symbol = ? ORDER BY timestamp LIMIT ? OFFSET ?",
                    conn, params=(symbol, batch_size, offset)
                )
                if df.empty:
                    break
                all_dfs.append(df)
                offset += batch_size
            
            if all_dfs:
                combined = pd.concat(all_dfs, ignore_index=True)
                write_candles_parquet(symbol, combined, interval="minute")
                stats["symbols_migrated"] += 1
                stats["total_rows"] += len(combined)
                print(f"  Migrated {symbol}: {len(combined)} rows")
            
        except Exception as e:
            stats["errors"].append(f"{symbol}: {e}")
            print(f"  ERROR migrating {symbol}: {e}")
    
    conn.close()
    return stats


def get_parquet_stats() -> dict:
    """Get statistics about parquet storage."""
    if not CANDLES_PARQUET_DIR.exists():
        return {"exists": False}
    
    symbols = [d.name.split("=")[1] for d in CANDLES_PARQUET_DIR.iterdir() if d.is_dir()]
    total_size = sum(f.stat().st_size for f in CANDLES_PARQUET_DIR.rglob("*.parquet"))
    
    return {
        "exists": True,
        "symbols": len(symbols),
        "symbol_list": symbols,
        "total_size_mb": round(total_size / (1024**2), 2),
        "path": str(CANDLES_PARQUET_DIR),
    }


def warmup_parquet_cache(symbols: List[str], interval: str = "minute") -> dict:
    """
    Pre-load parquet metadata for symbols to speed up first access.
    """
    _check_parquet()
    
    results = {"warmed": 0, "failed": []}
    engine = get_parquet_engine()
    
    for symbol in symbols:
        parquet_dir = symbol_parquet_dir(symbol, interval)
        if parquet_dir.exists():
            try:
                _ = pq.ParquetDataset(str(parquet_dir), engine=engine)
                results["warmed"] += 1
            except Exception as e:
                results["failed"].append(f"{symbol}: {e}")
        else:
            results["failed"].append(f"{symbol}: not found")
    
    return results


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python -m storage.parquet_store migrate     # Migrate SQLite -> Parquet")
        print("  python -m storage.parquet_store stats       # Show parquet stats")
        print("  python -m storage.parquet_store test SYMBOL # Test read for symbol")
        sys.exit(1)
    
    cmd = sys.argv[1]
    
    if cmd == "migrate":
        print("Migrating SQLite -> Parquet...")
        stats = migrate_sqlite_to_parquet()
        print(f"\nDone: {stats['symbols_migrated']} symbols, {stats['total_rows']} rows")
        if stats["errors"]:
            print(f"Errors: {stats['errors']}")
    
    elif cmd == "stats":
        stats = get_parquet_stats()
        print(f"Parquet stats: {stats}")
    
    elif cmd == "test" and len(sys.argv) > 2:
        symbol = sys.argv[2]
        df = read_candles_parquet(symbol, limit=10)
        print(f"{symbol}: {len(df)} rows")
        print(df.head())