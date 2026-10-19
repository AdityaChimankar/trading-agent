"""Parquet storage for efficient time-series data."""

from __future__ import annotations

import os
import re
from uuid import uuid4
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import numpy as np
from filelock import FileLock
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from trading_agent.core.config import get_settings
from trading_agent.core.logging_config import get_logger

logger = get_logger(__name__)


def _parse_utc_timestamps(values: pd.Series) -> pd.Series:
    """Parse timestamps written by current and legacy Parquet writers.

    Older candle files can contain ISO-8601 strings with a mixture of ``T`` or
    space separators and optional fractional seconds. Pandas 2 uses a strict
    format inferred from the first value unless ``format="mixed"`` is set.
    """
    if pd.api.types.is_datetime64_any_dtype(values.dtype):
        return pd.to_datetime(values, utc=True)
    return pd.to_datetime(values, utc=True, format="mixed")


class ParquetStore:
    """Partitioned Parquet storage for candle data."""

    def __init__(self, root_path: Path | None = None, compression: str = "snappy"):
        settings = get_settings()
        self.root_path = root_path or settings.storage.parquet_root
        self.compression = compression
        self.root_path.mkdir(parents=True, exist_ok=True)

    def _symbol_path(self, symbol: str) -> Path:
        """Get path for a symbol's partitioned data."""
        self._validate_key(symbol)
        return self.root_path / "candles" / f"symbol={symbol}"

    def _interval_path(self, symbol: str, interval: str) -> Path:
        """Get path for a symbol+interval."""
        self._validate_key(interval)
        return self._symbol_path(symbol) / f"interval={interval}"

    @staticmethod
    def _validate_key(value: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9&_.-]{1,64}", value) or ".." in value:
            raise ValueError("Invalid candle storage key")

    def _lock(self, symbol: str, interval: str) -> FileLock:
        self._interval_path(symbol, interval)
        locks = self.root_path / ".locks"
        locks.mkdir(parents=True, exist_ok=True)
        return FileLock(str(locks / f"{symbol}_{interval}.lock"), timeout=30)

    def _read_files(self, files: list[Path]) -> pd.DataFrame:
        """Read legacy string and Arrow timestamp schemas independently.

        Later committed files win on duplicate candle keys. Filtering must
        happen after deduplication so an obsolete candle cannot reappear.
        """
        frames = []
        for file in sorted(files, key=lambda p: (p.stat().st_mtime_ns, p.name)):
            frame = pq.ParquetFile(file).read().to_pandas()
            frame["timestamp"] = _parse_utc_timestamps(frame["timestamp"])
            frames.append(frame)
        if not frames:
            return pd.DataFrame()
        return (pd.concat(frames, ignore_index=True)
                .drop_duplicates("timestamp", keep="last")
                .sort_values("timestamp").reset_index(drop=True))

    def _commit_frame(self, frame: pd.DataFrame, directory: Path) -> Path:
        """Publish a complete immutable file; interrupted writes stay invisible."""
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"batch-{uuid4().hex}.parquet"
        temporary = destination.with_suffix(".tmp")
        try:
            table = pa.Table.from_pandas(frame, preserve_index=False)
            with temporary.open("wb") as handle:
                pq.write_table(table, handle, compression=self.compression)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

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

        df["timestamp"] = _parse_utc_timestamps(df["timestamp"])
        if df["timestamp"].isna().any():
            raise ValueError("Candle timestamps cannot be missing")
        prices = ["open", "high", "low", "close"]
        for col in prices + ["volume"]:
            df[col] = pd.to_numeric(df[col], errors="raise")
        if not np.isfinite(df[prices + ["volume"]].to_numpy()).all():
            raise ValueError("Candle values must be finite")
        if (df[prices] <= 0).any().any() or (df["volume"] < 0).any():
            raise ValueError("Candle prices must be positive and volume nonnegative")
        if ((df["high"] < df[prices].max(axis=1)) |
                (df["low"] > df[prices].min(axis=1))).any():
            raise ValueError("Candle OHLC bounds are inconsistent")
        df = df.drop_duplicates("timestamp", keep="last").sort_values("timestamp")

        # Add partition columns
        if partition_by is None:
            partition_by = ["year", "month"]
        if len(set(partition_by)) != len(partition_by) or set(partition_by) - {"year", "month", "day"}:
            raise ValueError("Partitions must be unique year/month/day fields")
        for col in partition_by:
            if col == "year":
                df[col] = df["timestamp"].dt.year
            elif col == "month":
                df[col] = df["timestamp"].dt.month
            elif col == "day":
                df[col] = df["timestamp"].dt.day

        path = self._interval_path(symbol, interval)
        with self._lock(symbol, interval):
            groups = df.groupby(partition_by, sort=True) if partition_by else [((), df)]
            for keys, group in groups:
                keys = keys if isinstance(keys, tuple) else (keys,)
                directory = path.joinpath(*(f"{col}={value}" for col, value in zip(partition_by, keys)))
                self._commit_frame(group, directory)

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

        with self._lock(symbol, interval):
            df = self._read_files(list(path.rglob("*.parquet")))
        if df.empty:
            return df
        if start is not None:
            df = df[df["timestamp"] >= pd.to_datetime(start, utc=True)]
        if end is not None:
            df = df[df["timestamp"] <= pd.to_datetime(end, utc=True)]
        operations = {">=": "ge", "<=": "le", ">": "gt", "<": "lt", "==": "eq", "!=": "ne"}
        for col, op, value in filters or []:
            if op not in operations:
                raise ValueError(f"Unsupported candle filter: {op}")
            if col == "timestamp":
                value = pd.to_datetime(value, utc=True)
            df = df[getattr(df[col], operations[op])(value)]
        return (df if columns is None else df[columns]).reset_index(drop=True)

    def read_latest(
        self,
        symbol: str,
        interval: str = "5minute",
        n: int = 1,
    ) -> pd.DataFrame:
        """Read the N most recent candles."""
        if n < 1:
            raise ValueError("n must be positive")
        return self.read_candles(symbol, interval).tail(n).reset_index(drop=True)

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
        df = self.read_candles(symbol, interval, columns=["timestamp"])

        if df.empty:
            return None, None

        df["timestamp"] = _parse_utc_timestamps(df["timestamp"])

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

        with self._lock(symbol, interval):
            originals = list(path.rglob("*.parquet"))
            frame = self._read_files(originals)
            if frame.empty:
                return 0
            # Publish the replacement before removing only the captured inputs.
            # A failed write leaves all originals available for recovery.
            self._commit_frame(frame, path)
            for original in originals:
                original.unlink()
        logger.info("parquet_compacted", symbol=symbol, interval=interval, rows=len(frame))
        return len(frame)

    def get_stats(self, symbol: str | None = None) -> dict:
        """Get storage statistics."""
        symbols = [symbol] if symbol else self.get_symbols()

        stats = {
            "row_count_basis": "physical rows; replay duplicates are deduplicated on reads",
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
