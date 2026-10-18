"""Parquet storage for efficient time-series data."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from trading_agent.core.config import get_settings
from trading_agent.core.logging_config import get_logger

logger = get_logger(__name__)


class ParquetStore:
    """Partitioned Parquet storage for candle data."""

    def __init__(self, root_path: Path | None = None, compression: str = "snappy"):
        settings = get_settings()
        self.root_path = root_path or settings.storage.parquet_root
        self.compression = compression
        self.root_path.mkdir(parents=True, exist_ok=True)

    def _symbol_path(self, symbol: str) -> Path:
        """Get path for a symbol's partitioned data."""
        return self.root_path / "candles" / f"symbol={symbol}"

    def _interval_path(self, symbol: str, interval: str) -> Path:
        """Get path for a symbol+interval."""
        return self.root_path / "candles" / f"symbol={symbol}" / f"interval={interval}"

    def write_candles(
        self,
        df: pd.DataFrame,
        symbol: str,
        interval: str = "5minute",
        partition_by: list[str] | None = None,
    ) -> int:
        """Write candles to partitioned Parquet.

        Args:
            df: DataFrame with columns [timestamp, open, high, low, close, volume, oi?]
            symbol: Trading symbol
            interval: Candle interval
            partition_by: Additional partition columns (e.g., ['year', 'month'])

        Returns:
            Number of rows written
        """
        if df.empty:
            return 0

        # Ensure required columns
        required = ["timestamp", "open", "high", "low", "close", "volume"]
        for col in required:
            if col not in df.columns:
                raise ValueError(f"Missing required column: {col}")

        # Copy and prepare
        df = df.copy()
        df["symbol"] = symbol
        df["interval"] = interval

        # Ensure timestamp is timezone-aware UTC
        if df["timestamp"].dt.tz is None:
            df["timestamp"] = df["timestamp"].dt.tz_localize(UTC)
        else:
            df["timestamp"] = df["timestamp"].dt.tz_convert(UTC)

        # Add partition columns
        if partition_by is None:
            partition_by = ["year", "month"]
        for col in partition_by:
            if col == "year":
                df[col] = df["timestamp"].dt.year
            elif col == "month":
                df[col] = df["timestamp"].dt.month
            elif col == "day":
                df[col] = df["timestamp"].dt.day

        # Convert to Arrow table
        table = pa.Table.from_pandas(df, preserve_index=False)

        # Write partitioned dataset
        path = self._interval_path(symbol, interval)
        path.mkdir(parents=True, exist_ok=True)

        pq.write_to_dataset(
            table,
            root_path=str(path),
            partition_cols=partition_by,
            compression=self.compression,
            existing_data_behavior="overwrite_or_ignore",
            basename_template=f"{symbol}_{interval}_part-{{i}}.parquet",
        )

        logger.info("parquet_write_complete", symbol=symbol, interval=interval, rows=len(df))
        return len(df)

    def read_candles(
        self,
        symbol: str,
        interval: str = "5minute",
        start: datetime | None = None,
        end: datetime | None = None,
        columns: list[str] | None = None,
        filters: list | None = None,
    ) -> pd.DataFrame:
        """Read candles from Parquet with optional filtering.

        Args:
            symbol: Trading symbol
            interval: Candle interval
            start: Start timestamp (inclusive)
            end: End timestamp (inclusive)
            columns: Columns to read (None for all)
            filters: Additional pyarrow dataset filters

        Returns:
            DataFrame with candle data
        """
        path = self._interval_path(symbol, interval)
        if not path.exists():
            return pd.DataFrame()

        # Build filters as pyarrow expressions
        # Note: timestamps are stored as strings in ISO format, so we need string comparison
        import pyarrow.compute as pc
        ds_filter = None
        
        # Convert datetime to ISO string for comparison
        if start:
            start_str = start.isoformat()
            ds_filter = pc.field("timestamp") >= start_str
        if end:
            end_str = end.isoformat()
            end_filter = pc.field("timestamp") <= end_str
            ds_filter = ds_filter & end_filter if ds_filter is not None else end_filter
        if filters:
            for f in filters:
                if isinstance(f, tuple) and len(f) == 3:
                    col, op, val = f
                    if op == ">=":
                        f_expr = pc.field(col) >= val
                    elif op == "<=":
                        f_expr = pc.field(col) <= val
                    elif op == ">":
                        f_expr = pc.field(col) > val
                    elif op == "<":
                        f_expr = pc.field(col) < val
                    elif op == "==":
                        f_expr = pc.field(col) == val
                    else:
                        continue
                    ds_filter = ds_filter & f_expr if ds_filter is not None else f_expr

        dataset = ds.dataset(str(path), format="parquet")
        table = dataset.to_table(columns=columns, filter=ds_filter)
        df = table.to_pandas()

        if not df.empty:
            # Convert timestamp from string to datetime
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
            df = df.sort_values("timestamp").reset_index(drop=True)

        return df

    def read_latest(
        self,
        symbol: str,
        interval: str = "5minute",
        n: int = 1,
    ) -> pd.DataFrame:
        """Read the N most recent candles."""
        path = self._interval_path(symbol, interval)
        if not path.exists():
            return pd.DataFrame()

        dataset = ds.dataset(str(path), format="parquet")
        # Read all and take last N (could be optimized with scanner)
        table = dataset.to_table()
        df = table.to_pandas()

        if df.empty:
            return df

        # Convert timestamp from string to datetime
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

        df = df.sort_values("timestamp").tail(n).reset_index(drop=True)
        return df

    def get_symbols(self) -> list[str]:
        """Get list of symbols with data."""
        candles_path = self.root_path / "candles"
        if not candles_path.exists():
            return []

        symbols = []
        for item in candles_path.iterdir():
            if item.is_dir() and item.name.startswith("symbol="):
                symbols.append(item.name[7:])  # Remove "symbol="
        return sorted(symbols)

    def get_intervals(self, symbol: str) -> list[str]:
        """Get available intervals for a symbol."""
        path = self._symbol_path(symbol)
        if not path.exists():
            return []

        intervals = []
        for item in path.iterdir():
            if item.is_dir() and item.name.startswith("interval="):
                intervals.append(item.name[9:])  # Remove "interval="
        return sorted(intervals)

    def get_date_range(
        self,
        symbol: str,
        interval: str = "5minute",
    ) -> tuple[datetime | None, datetime | None]:
        """Get min/max timestamps for a symbol+interval."""
        path = self._interval_path(symbol, interval)
        if not path.exists():
            return None, None

        dataset = ds.dataset(str(path), format="parquet")
        # Only read timestamp column for efficiency
        table = dataset.to_table(columns=["timestamp"])
        df = table.to_pandas()

        if df.empty:
            return None, None

        if df["timestamp"].dt.tz is None:
            df["timestamp"] = df["timestamp"].dt.tz_localize(UTC)

        return df["timestamp"].min().to_pydatetime(), df["timestamp"].max().to_pydatetime()

    def delete_symbol(self, symbol: str) -> bool:
        """Delete all data for a symbol."""
        import shutil

        path = self._symbol_path(symbol)
        if path.exists():
            shutil.rmtree(path)
            logger.info("parquet_symbol_deleted", symbol=symbol)
            return True
        return False

    def compact(self, symbol: str, interval: str = "5minute") -> int:
        """Compact small files into larger ones."""
        path = self._interval_path(symbol, interval)
        if not path.exists():
            return 0

        dataset = ds.dataset(str(path), format="parquet")
        table = dataset.to_table()

        if table.num_rows == 0:
            return 0

        # Remove old files
        import shutil

        shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)

        # Rewrite as single file (or fewer files)
        pq.write_table(
            table,
            path / f"{symbol}_{interval}.parquet",
            compression=self.compression,
        )

        logger.info("parquet_compacted", symbol=symbol, interval=interval, rows=table.num_rows)
        return table.num_rows

    def get_stats(self, symbol: str | None = None) -> dict:
        """Get storage statistics."""
        symbols = [symbol] if symbol else self.get_symbols()

        stats = {
            "symbols": len(symbols),
            "total_rows": 0,
            "total_size_mb": 0.0,
            "by_symbol": {},
        }

        for sym in symbols:
            sym_path = self._symbol_path(sym)
            if not sym_path.exists():
                continue

            sym_rows = 0
            sym_size = 0
            for root, _dirs, files in os.walk(sym_path):
                for file in files:
                    if file.endswith(".parquet"):
                        filepath = Path(root) / file
                        sym_size += filepath.stat().st_size
                        # Quick row count (approximate)
                        try:
                            pf = pq.ParquetFile(filepath)
                            sym_rows += pf.metadata.num_rows
                        except Exception:
                            pass

            stats["by_symbol"][sym] = {
                "rows": sym_rows,
                "size_mb": round(sym_size / (1024 * 1024), 2),
            }
            stats["total_rows"] += sym_rows
            stats["total_size_mb"] += stats["by_symbol"][sym]["size_mb"]

        stats["total_size_mb"] = round(stats["total_size_mb"], 2)
        return stats


# Global instance
_store: ParquetStore | None = None


def get_parquet_store(root_path: Path | None = None, compression: str = "snappy") -> ParquetStore:
    """Get or create the global Parquet store."""
    global _store
    if _store is None:
        _store = ParquetStore(root_path, compression)
    return _store
