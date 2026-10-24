import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from trading_agent.analysis import candle_changes as changes


@pytest.fixture
def storage(tmp_path, monkeypatch):
    changes._cache.clear()
    monkeypatch.setattr(changes, "get_settings", lambda: SimpleNamespace(
        storage=SimpleNamespace(parquet_root=tmp_path)))
    yield tmp_path
    changes._cache.clear()


def write_file(storage, symbol, timestamps, name="batch.parquet", interval="minute"):
    path = storage / "candles" / f"symbol={symbol}" / f"interval={interval}" / "year=2026" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"timestamp": timestamps, "close": [100.0] * len(timestamps)}).to_parquet(path, index=False)
    return path


def test_observes_all_symbols_missing_and_mixed_legacy_timestamps(storage):
    now = datetime(2026, 10, 21, 4, 1, 5, tzinfo=UTC)
    write_file(storage, "A", ["2026-10-21T03:59:00", "2026-10-21 04:00:00.000000Z"])
    write_file(storage, "Z", pd.to_datetime(["2026-10-21T04:00:00Z"], utc=True))
    write_file(storage, "OLD", ["2026-10-20 04:00:00Z"])
    result = changes.observe_candles(["Z", "MISSING", "A", "A", "OLD"], "minute", now)
    assert result["configured_symbols"] == 4
    assert result["stored_symbols"] == 3
    assert result["fresh_symbols"] == 2
    assert result["latest_completed_at"] == "2026-10-21T04:00:00+00:00"
    assert changes.observe_candles(["OLD", "A", "MISSING", "Z"], "minute", now) == result


def test_only_committed_completed_files_affect_fingerprint(storage):
    now = datetime(2026, 10, 21, 4, 1, 5, tzinfo=UTC)
    empty = changes.observe_candles(["A"], "minute", now)
    write_file(storage, "A", ["2026-10-21T04:02:00Z"], "future.parquet")
    partial = write_file(storage, "A", ["2026-10-21T04:00:00Z"], "writing.tmp")
    assert partial.exists()
    assert changes.observe_candles(["A"], "minute", now) == empty
    complete = changes.observe_candles(["A"], "minute", now + timedelta(minutes=2))
    assert complete["fingerprint"] != empty["fingerprint"]
    assert complete["fresh_symbols"] == 1
    assert complete["latest_completed_at"] == "2026-10-21T04:02:00+00:00"


def test_future_only_new_file_does_not_retrigger_existing_fresh_bar(storage):
    now = datetime(2026, 10, 21, 4, 1, 5, tzinfo=UTC)
    write_file(storage, "A", ["2026-10-21T04:00:00Z"], "complete.parquet")
    before = changes.observe_candles(["A"], "minute", now)
    write_file(storage, "A", ["2026-10-21T04:01:00Z"], "building.parquet")
    assert changes.observe_candles(["A"], "minute", now) == before
    after = changes.observe_candles(["A"], "minute", now + timedelta(minutes=1))
    assert after["fingerprint"] != before["fingerprint"]
    assert after["latest_completed_at"] == "2026-10-21T04:01:00+00:00"


def test_stable_cache_and_new_completions_without_file_change(storage, monkeypatch):
    now = datetime(2026, 10, 21, 4, 1, 5, tzinfo=UTC)
    write_file(storage, "A", ["2026-10-21T04:00:00Z", "2026-10-21T04:01:00Z"])
    first = changes.observe_candles(["A"], "minute", now)
    original = changes.pq.ParquetFile
    calls = []

    def count_reads(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(changes.pq, "ParquetFile", count_reads)
    assert changes.observe_candles(["A"], "minute", now + timedelta(seconds=10)) == first
    assert calls == []
    second = changes.observe_candles(["A"], "minute", now + timedelta(minutes=1))
    assert len(calls) == 1
    assert second["fingerprint"] != first["fingerprint"]
    assert second["latest_completed_at"] == "2026-10-21T04:01:00+00:00"
    # A backward clock must not reuse a future completed observation.
    assert changes.observe_candles(["A"], "minute", now) == first


def test_typed_complete_files_use_statistics_not_full_column_reads(storage, monkeypatch):
    now = datetime(2026, 10, 21, 4, 1, 5, tzinfo=UTC)
    write_file(storage, "A", pd.to_datetime(["2026-10-21T03:59:00Z", "2026-10-21T04:00:00Z"], utc=True))

    def forbidden(*args, **kwargs):
        raise AssertionError("Complete typed file should not decode timestamp rows")

    monkeypatch.setattr(changes.pq.ParquetFile, "iter_batches", forbidden)
    result = changes.observe_candles(["A"], "minute", now)
    assert result["fresh_symbols"] == 1
    assert result["latest_completed_at"] == "2026-10-21T04:00:00+00:00"


def test_typed_partial_groups_and_nulls_fall_back_to_actual_timestamps(storage):
    now = datetime(2026, 10, 21, 4, 1, 5, tzinfo=UTC)
    write_file(storage, "A", pd.to_datetime(["2026-10-21T04:00:00Z", "2026-10-21T04:01:00Z"], utc=True))
    first = changes.observe_candles(["A"], "minute", now)
    assert first["latest_completed_at"] == "2026-10-21T04:00:00+00:00"
    assert changes.observe_candles(["A"], "minute", now + timedelta(minutes=1))["fingerprint"] != first["fingerprint"]
    write_file(storage, "A", pd.to_datetime(["2026-10-21T04:00:00Z", None], utc=True), name="null.parquet")
    with pytest.raises(changes.CandleObservationError, match="missing timestamps"):
        changes.observe_candles(["A"], "minute", now)


def test_corrections_compaction_and_stale_history(storage):
    now = datetime(2026, 10, 21, 4, 1, 5, tzinfo=UTC)
    path = write_file(storage, "A", ["2026-10-20T04:00:00Z"])
    first = changes.observe_candles(["A"], "minute", now)
    assert first["fresh_symbols"] == 0
    frame = pd.read_parquet(path)
    frame["close"] = 123.456
    before = path.stat().st_mtime_ns
    frame.to_parquet(path, index=False)
    # Same size, and on a fast disk possibly the same mtime tick: make the rewrite observable.
    os.utime(path, ns=(before + 1_000_000_000, before + 1_000_000_000))
    second = changes.observe_candles(["A"], "minute", now)
    assert second["fingerprint"] != first["fingerprint"]
    assert second["fresh_symbols"] == 0
    path.rename(path.with_name("compacted.parquet"))
    assert changes.observe_candles(["A"], "minute", now)["fingerprint"] != second["fingerprint"]


def test_missing_or_corrupt_timestamps_fail_closed(storage):
    now = datetime(2026, 10, 21, 4, 1, 5, tzinfo=UTC)
    write_file(storage, "A", ["not a timestamp"])
    with pytest.raises(changes.CandleObservationError, match="could not be verified"):
        changes.observe_candles(["A"], "minute", now)


def test_compaction_race_retries_and_never_returns_partial_universe(storage, monkeypatch):
    now = datetime(2026, 10, 21, 4, 1, 5, tzinfo=UTC)
    write_file(storage, "A", ["2026-10-21T04:00:00Z"])
    original = changes._summarize
    attempts = []

    def race(*args):
        attempts.append(1)
        if len(attempts) == 1:
            raise FileNotFoundError()
        return original(*args)

    monkeypatch.setattr(changes, "_summarize", race)
    assert changes.observe_candles(["A"], "minute", now)["fresh_symbols"] == 1
    assert len(attempts) == 2
    monkeypatch.setattr(changes, "_summarize", lambda *args: (_ for _ in ()).throw(FileNotFoundError()))
    with pytest.raises(changes.CandleObservationError, match="changed during observation"):
        changes.observe_candles(["A"], "minute", now)


def test_work_and_cache_are_bounded(storage, monkeypatch):
    now = datetime(2026, 10, 21, 4, 1, 5, tzinfo=UTC)
    for symbol in ["A", "B", "C"]:
        write_file(storage, symbol, ["2026-10-21T04:00:00Z"])
    monkeypatch.setattr(changes, "MAX_CACHE_ENTRIES", 2)
    assert changes.observe_candles(["A", "B", "C"], "minute", now)["fresh_symbols"] == 3
    assert len(changes._cache) == 2
    monkeypatch.setattr(changes, "MAX_FILES", 2)
    with pytest.raises(changes.CandleObservationError, match="file budget"):
        changes.observe_candles(["A", "B", "C"], "minute", now)
    monkeypatch.setattr(changes, "MAX_OBSERVATION_SECONDS", -1)
    with pytest.raises(changes.CandleObservationError, match="time budget"):
        changes.observe_candles(["A", "B", "C"], "minute", now)


def test_invalid_keys_and_naive_clock_are_rejected_without_writes(storage):
    now = datetime(2026, 10, 21, 4, 1, 5, tzinfo=UTC)
    with pytest.raises(ValueError):
        changes.observe_candles(["../other"], "minute", now)
    with pytest.raises(ValueError):
        changes.observe_candles(["A"], "minute", now.replace(tzinfo=None))
    assert list(storage.iterdir()) == []


def test_cold_cache_progress_is_pending_not_partial_coverage(storage, monkeypatch):
    now = datetime(2026, 10, 21, 4, 1, 5, tzinfo=UTC)
    for symbol in ("A", "B"):
        write_file(storage, symbol, ["2026-10-21T04:00:00Z"])
    original = changes._summarize

    def bounded(path, *args):
        if "symbol=B" in str(path):
            raise changes._TimeBudgetExceeded("time budget")
        return original(path, *args)

    monkeypatch.setattr(changes, "_summarize", bounded)
    with pytest.raises(changes.CandleObservationPending) as result:
        changes.observe_candles(["A", "B"], "minute", now)
    assert result.value.cached_files == 1
    # No additional progress must count as an actual capacity failure.
    with pytest.raises(changes._TimeBudgetExceeded):
        changes.observe_candles(["A", "B"], "minute", now)
