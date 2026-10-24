"""Read-only, bounded observations of committed local candle files.

This is a scheduling hint, never permission to trade. The research engine still
loads, deduplicates and validates actual candles. Only published ``.parquet``
files count; incomplete/future-only files do not advance the fingerprint.

Cold observations enumerate the configured universe and inspect typed timestamp
statistics, falling back to timestamp columns for legacy or partly pending files.
Warm observations reuse bounded per-file summaries, including the next timestamp
that will become complete. Corrections and compaction conservatively invalidate
the fingerprint. Very fragmented stores can exceed the explicit work budgets:
the caller must report the failure, not treat partial coverage as a full scan.
No broker/provider requests, storage creation or candle writes occur here.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import threading
import time

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from trading_agent.core.config import get_settings
from trading_agent.storage.parquet_store import ParquetStore

SECONDS = {"minute": 60, "5minute": 300, "15minute": 900, "day": 86400}
MAX_FILES = 1_000_000
MAX_CACHE_ENTRIES = 100_000
MAX_ROWS_PER_FILE = 5_000_000
MAX_OBSERVATION_SECONDS = 15.0


class CandleObservationError(RuntimeError):
    """No complete, reliable observation was available within the work budget."""


class CandleObservationPending(CandleObservationError):
    """Bounded cold-cache progress, not a successful/partial observation."""

    def __init__(self, cached_files):
        self.cached_files = cached_files
        super().__init__("Preparing committed candle summaries")


class _TimeBudgetExceeded(CandleObservationError):
    pass


class _ChangedDuringObservation(Exception):
    pass


@dataclass(frozen=True)
class _FileSummary:
    version: tuple[int, int]
    cutoff: datetime
    completed_rows: int
    latest_completed: datetime | None
    next_pending: datetime | None


_cache: OrderedDict[str, _FileSummary] = OrderedDict()
_lock = threading.Lock()


def _check_budget(started: float) -> None:
    if time.monotonic() - started > MAX_OBSERVATION_SECONDS:
        raise _TimeBudgetExceeded(
            "Candle observation exceeded its time budget; fragmented or slow local storage needs review"
        )


def _version(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_mtime_ns, info.st_size


def _typed_summary(parquet, cutoff):
    """Fast scheduling hint for immutable typed files; never validates OHLCV.

    String statistics cannot be ordered chronologically for mixed ISO formats.
    Missing statistics, nulls, and partly completed row groups use the actual
    timestamp column instead. No file size/mtime is a freshness assertion.
    """
    if not pa.types.is_timestamp(parquet.schema_arrow.field("timestamp").type):
        return None
    index = parquet.schema.names.index("timestamp")
    completed_rows = 0
    latest_completed = next_pending = None
    for number in range(parquet.metadata.num_row_groups):
        group = parquet.metadata.row_group(number)
        stats = group.column(index).statistics
        if stats is None or not stats.has_min_max or stats.null_count != 0:
            return None
        low = pd.to_datetime(stats.min, utc=True).to_pydatetime()
        high = pd.to_datetime(stats.max, utc=True).to_pydatetime()
        if low > high:
            return None
        if high <= cutoff:
            completed_rows += group.num_rows
            latest_completed = max(latest_completed, high) if latest_completed else high
        elif low > cutoff:
            next_pending = min(next_pending, low) if next_pending else low
        else:
            return None
    return completed_rows, latest_completed, next_pending


def _summarize(path: Path, cutoff: datetime, started: float, version=None) -> _FileSummary:
    version = version or _version(path)
    key = str(path)
    cached = _cache.get(key)
    if (cached and cached.version == version and cutoff >= cached.cutoff
            and (cached.next_pending is None or cached.next_pending > cutoff)):
        _cache.move_to_end(key)
        return cached

    completed_rows = 0
    latest_completed = next_pending = None
    with pq.ParquetFile(path) as parquet:
        if parquet.metadata.num_rows > MAX_ROWS_PER_FILE:
            raise CandleObservationError("Candle file exceeds the timestamp observation row budget")
        if "timestamp" not in parquet.schema_arrow.names:
            raise CandleObservationError("Committed candle file is missing timestamps")
        typed = _typed_summary(parquet, cutoff)
        if typed is not None:
            completed_rows, latest_completed, next_pending = typed
        for batch in (() if typed is not None else parquet.iter_batches(batch_size=65536, columns=["timestamp"])):
            _check_budget(started)
            values = batch.column(0).to_pandas()
            if pd.api.types.is_numeric_dtype(values.dtype):
                raise CandleObservationError("Numeric candle timestamps are not a supported storage contract")
            stamps = pd.to_datetime(values, utc=True, format="mixed")
            if stamps.isna().any():
                raise CandleObservationError("Committed candle file contains missing timestamps")
            completed = stamps[stamps <= cutoff]
            pending = stamps[stamps > cutoff]
            completed_rows += len(completed)
            if len(completed):
                candidate = completed.max().to_pydatetime()
                latest_completed = max(latest_completed, candidate) if latest_completed else candidate
            if len(pending):
                candidate = pending.min().to_pydatetime()
                next_pending = min(next_pending, candidate) if next_pending else candidate
    if _version(path) != version:
        raise _ChangedDuringObservation()
    result = _FileSummary(version, cutoff, completed_rows, latest_completed, next_pending)
    _cache[key] = result
    _cache.move_to_end(key)
    while len(_cache) > MAX_CACHE_ENTRIES:
        _cache.popitem(last=False)
    return result


def _walk_files(directory: Path, missing_ok=True):
    """Use cached DirEntry metadata: avoid a filesystem open per cached file.

    This matters on Windows, where repeated Path.stat/is_symlink calls across
    every historical partition otherwise dominate even a warm observation.
    Only new/changed files need the second stat after decoding to detect races.
    """
    try:
        with os.scandir(directory) as handle:
            entries = sorted(handle, key=lambda entry: entry.name)
    except FileNotFoundError:
        if missing_ok:
            return
        raise
    for entry in entries:
        if entry.is_symlink():
            raise CandleObservationError("Symlinked candle partitions/files cannot be observed safely")
        if entry.is_dir(follow_symlinks=False):
            yield from _walk_files(Path(entry.path), missing_ok=False)
        elif entry.name.endswith(".parquet"):
            info = entry.stat(follow_symlinks=False)
            yield Path(entry.path), (info.st_mtime_ns, info.st_size)


def _observe(root: Path, symbols: list[str], interval: str, now: datetime, started: float) -> dict:
    cutoff = now - timedelta(seconds=SECONDS[interval])
    fresh_after = now - timedelta(seconds=SECONDS[interval] * 2 + 15)
    digest = hashlib.sha256()
    digest.update(json.dumps([interval, symbols], separators=(",", ":")).encode())
    stored_symbols = fresh_symbols = file_count = 0
    latest_completed = None
    for symbol in symbols:
        _check_budget(started)
        directory = root / "candles" / f"symbol={symbol}" / f"interval={interval}"
        if directory.is_symlink() or directory.parent.is_symlink():
            raise CandleObservationError("Symlinked candle partitions cannot be observed safely")
        symbol_latest = None
        for path, version in _walk_files(directory):
            file_count += 1
            if file_count > MAX_FILES:
                raise CandleObservationError("Candle observation file budget exceeded; review storage fragmentation")
            _check_budget(started)
            summary = _summarize(path, cutoff, started, version)
            if summary.latest_completed is not None:
                symbol_latest = max(symbol_latest, summary.latest_completed) if symbol_latest else summary.latest_completed
                record = [str(path.relative_to(root)), summary.version, summary.completed_rows,
                          summary.latest_completed.isoformat()]
                digest.update(json.dumps(record, separators=(",", ":")).encode())
                digest.update(b"\n")
        if symbol_latest is not None:
            stored_symbols += 1
            fresh_symbols += int(symbol_latest > fresh_after)
            latest_completed = max(latest_completed, symbol_latest) if latest_completed else symbol_latest
    return {
        "fingerprint": digest.hexdigest(),
        "fresh_symbols": fresh_symbols,
        "stored_symbols": stored_symbols,
        "configured_symbols": len(symbols),
        "latest_completed_at": latest_completed.isoformat() if latest_completed else None,
    }


def observe_candles(symbols: list[str], interval: str, now: datetime) -> dict:
    """Observe every configured symbol, or fail without returning partial results.

    A disappearing/rewritten file (e.g. concurrent compaction) gets one retry.
    A new commit arriving after traversal is seen on the next observation; this
    cannot authorize an entry because the scan separately validates the data.
    The cache retains only scalar summaries, never historical timestamp arrays.
    """
    if interval not in SECONDS:
        raise ValueError("Unsupported candle observation interval")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Candle observation requires a timezone-aware clock")
    symbols = sorted(set(symbols))
    for symbol in symbols:
        ParquetStore._validate_key(symbol)
    root = get_settings().storage.parquet_root.resolve()
    now = now.astimezone(UTC)
    started = time.monotonic()
    with _lock:
        previous_cached = len(_cache)
        for attempt in range(2):
            try:
                return _observe(root, symbols, interval, now, started)
            except (FileNotFoundError, _ChangedDuringObservation) as error:
                if attempt:
                    raise CandleObservationError("Candle files changed during observation; retry on the next cycle") from error
            except _TimeBudgetExceeded as error:
                if len(_cache) > previous_cached:
                    raise CandleObservationPending(len(_cache)) from error
                raise
            except CandleObservationError:
                raise
            except (OSError, ValueError, TypeError) as error:
                raise CandleObservationError("Committed candle timestamps could not be verified") from error
    raise CandleObservationError("Candle observation could not be completed")
