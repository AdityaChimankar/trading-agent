"""Parquet storage for efficient time-series data."""

from __future__ import annotations

import os
import re
import threading
from collections import OrderedDict
from typing import NamedTuple
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


_OFFSET_SUFFIX = r"(?:Z|[+-]\d{2}:?\d{2})$"


def _parse_stored_timestamps(values: pd.Series) -> pd.Series:
    """Parse timestamps read back from candle files.

    Every current write is tz-aware UTC (``write_candles``). Files from the old
    pipeline hold naive wall-clock strings in IST, its "canonical naive IST
    format" (scripts/migrate_legacy_timestamps.py). Reading those as UTC moved
    each legacy session 5h30m late and let a candle appear twice once newer
    files held the same instant. So stored values without an offset are IST;
    values with an offset keep it. Nothing is rewritten on disk.
    """
    if isinstance(values.dtype, pd.DatetimeTZDtype):
        return pd.to_datetime(values, utc=True)
    if pd.api.types.is_datetime64_any_dtype(values.dtype):
        return values.dt.tz_localize("Asia/Kolkata").dt.tz_convert("UTC")
    text = values.astype("string")
    aware = text.str.contains(_OFFSET_SUFFIX, regex=True).fillna(False).astype(bool)
    parts = []
    if aware.any():
        parts.append(pd.to_datetime(text[aware], utc=True, format="mixed"))
    if (~aware).any():
        parts.append(pd.to_datetime(text[~aware], format="mixed").dt.tz_localize("Asia/Kolkata").dt.tz_convert("UTC"))
    return pd.concat(parts).sort_index().astype("datetime64[ns, UTC]")


def _commit_order(files) -> list[tuple[tuple[int, str], Path]]:
    """Files oldest commit first; later files win on duplicate candle keys."""
    return sorted(((file.stat().st_mtime_ns, file.name), file) for file in files)


class _CachedFrame(NamedTuple):
    files: frozenset[Path]
    last: tuple[int, str]
    frame: pd.DataFrame


class ParquetStore:
    """Partitioned Parquet storage for candle data."""

    def __init__(self, root_path: Path | None = None, compression: str = "snappy",
                 cache_rows: int | None = None):
        settings = get_settings()
        self.root_path = root_path or settings.storage.parquet_root
        self.compression = compression
        self.root_path.mkdir(parents=True, exist_ok=True)
        # Deduplicated frames per (symbol, interval), least recently used first.
        # Files are immutable, so a read only opens files committed since.
        self.cache_rows = settings.storage.read_cache_rows if cache_rows is None else cache_rows
        self._cache: OrderedDict[tuple[str, str], _CachedFrame] = OrderedDict()
        self._cached_rows = 0
        self._cache_guard = threading.Lock()

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
        return self._read_ordered([file for _, file in _commit_order(files)])

    @staticmethod
    def _read_ordered(files: list[Path], base: pd.DataFrame | None = None) -> pd.DataFrame:
        frames = [] if base is None else [base]
        for file in files:
            frame = pq.ParquetFile(file).read().to_pandas()
            frame["timestamp"] = _parse_stored_timestamps(frame["timestamp"])
            frames.append(frame)
        if not frames:
            return pd.DataFrame()
        return (pd.concat(frames, ignore_index=True)
                .drop_duplicates("timestamp", keep="last")
                .sort_values("timestamp").reset_index(drop=True))

    def _read_interval(self, symbol: str, interval: str, path: Path) -> pd.DataFrame:
        """Every candle for one interval, deduplicated. Callers must not mutate it.

        Holds the interval lock, so no writer or compaction runs meanwhile. The
        cached frame is extended only when every new file commits after every
        cached one; otherwise (a compaction, a deleted file, a clock step) the
        interval is read in full, exactly as without the cache.
        """
        key = (symbol, interval)
        files = list(path.rglob("*.parquet"))
        with self._cache_guard:
            cached = self._cache.get(key)
        if cached is not None and cached.files.issubset(files):
            new = _commit_order(set(files) - cached.files)
            if not new:
                self._remember(key, cached)
                return cached.frame
            if new[0][0] > cached.last:
                frame = self._read_ordered([file for _, file in new], cached.frame)
                self._remember(key, _CachedFrame(frozenset(files), new[-1][0], frame))
                return frame
        ordered = _commit_order(files)
        frame = self._read_ordered([file for _, file in ordered])
        if ordered:
            self._remember(key, _CachedFrame(frozenset(files), ordered[-1][0], frame))
        else:
            self._forget(symbol, interval)
        return frame

    def _remember(self, key: tuple[str, str], entry: _CachedFrame) -> None:
        with self._cache_guard:
            old = self._cache.pop(key, None)
            self._cached_rows -= len(old.frame) if old is not None else 0
            if len(entry.frame) > self.cache_rows:
                return
            self._cache[key] = entry
            self._cached_rows += len(entry.frame)
            while self._cached_rows > self.cache_rows:
                _, evicted = self._cache.popitem(last=False)
                self._cached_rows -= len(evicted.frame)

    def _forget(self, symbol: str, interval: str | None = None) -> None:
        with self._cache_guard:
            for key in [k for k in self._cache if k[0] == symbol and interval in (None, k[1])]:
                self._cached_rows -= len(self._cache.pop(key).frame)

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
            self._forget(symbol, interval)
            return pd.DataFrame()

        with self._lock(symbol, interval):
            df = self._read_interval(symbol, interval, path)
        if df.empty:
            return df.copy()
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
        # A copy: the unfiltered frame is the cached one.
        return (df if columns is None else df[columns]).reset_index(drop=True).copy()

    def read_latest(
        self,
        symbol: str,
        interval: str = "5minute",
        n: int = 1,
    ) -> pd.DataFrame:
        """Read the N most recent candles."""
        if n < 1:
            raise ValueError("n must be positive")
        path = self._interval_path(symbol, interval)
        if not path.exists():
            self._forget(symbol, interval)
            return pd.DataFrame()
        with self._lock(symbol, interval):
            df = self._read_interval(symbol, interval, path)
        return df.tail(n).reset_index(drop=True).copy()

    def get_symbols(self) -> list[str]:
        """Get list of symbols with data."""
        candles_path = self.root_path / "candles"
        if not candles_path.exists():
            return []

        with os.scandir(candles_path) as entries:
            symbols = [item.name[7:] for item in entries
                       if item.name.startswith("symbol=") and item.is_dir()]
        return sorted(symbols)

    def get_intervals(self, symbol: str) -> list[str]:
        """Get available intervals for a symbol."""
        path = self._symbol_path(symbol)
        if not path.exists():
            return []

        with os.scandir(path) as entries:
            intervals = [item.name[9:] for item in entries
                         if item.name.startswith("interval=") and item.is_dir()]
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
        self._forget(symbol)
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
            # Emptied partitions stay listed by every read: 250 of them made one
            # symbol's minute read take 2.6 s on the Docker Desktop bind mount.
            for directory, _, _ in sorted(os.walk(path), reverse=True):
                if Path(directory) != path and not os.listdir(directory):
                    os.rmdir(directory)
            self._forget(symbol, interval)
        logger.info("parquet_compacted", symbol=symbol, interval=interval, rows=len(frame))
        return len(frame)

    def file_count(self, symbol: str, interval: str) -> int:
        """Committed files for one interval; every read opens or lists each of them."""
        path = self._interval_path(symbol, interval)
        return sum(1 for _ in path.rglob("*.parquet")) if path.exists() else 0

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
