"""Candle persistence invariants across live ingestion and historical repairs."""
import os
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from trading_agent.storage.parquet_store import ParquetStore


def candles(start="2026-10-19T03:45:00Z", n=2, close=100.0):
    return pd.DataFrame({"timestamp": pd.date_range(start, periods=n, freq="min"),
                         "open": 100.0, "high": 110.0, "low": 90.0,
                         "close": close, "volume": 10})


def test_incremental_writes_preserve_history_and_retries_replace_only_matching_keys(tmp_path):
    store = ParquetStore(tmp_path)
    store.write_candles(candles(), "TEST", "minute")
    store.write_candles(candles("2026-10-19T03:47:00Z"), "TEST", "minute")
    store.write_candles(candles(n=1, close=105), "TEST", "minute")
    actual = store.read_candles("TEST", "minute")
    assert len(actual) == 4
    assert actual["close"].tolist() == [105, 100, 100, 100]
    assert store.compact("TEST", "minute") == 4
    pd.testing.assert_frame_equal(actual, store.read_candles("TEST", "minute"))


def test_mixed_file_schemas_filter_by_instants_and_support_column_projection(tmp_path):
    store = ParquetStore(tmp_path)
    path = tmp_path / "candles" / "symbol=TEST" / "interval=minute"
    path.mkdir(parents=True)
    legacy = candles()
    legacy["timestamp"] = ["2026-10-19T09:15:00+05:30", "2026-10-19 03:46:00.000000Z"]
    pq.write_table(pa.Table.from_pandas(legacy), path / "data.parquet")
    store.write_candles(candles("2026-10-19T03:47:00Z"), "TEST", "minute")
    result = store.read_candles("TEST", "minute", start=pd.Timestamp("2026-10-19T09:16:00+05:30"),
                                end=pd.Timestamp("2026-10-19T03:47:00Z"), columns=["close"])
    assert result.columns.tolist() == ["close"]
    assert len(result) == 2
    assert store.get_date_range("TEST", "minute")[0] == pd.Timestamp("2026-10-19T03:45:00Z")


def test_failed_compaction_keeps_original_files(tmp_path, monkeypatch):
    store = ParquetStore(tmp_path)
    store.write_candles(candles(), "TEST", "minute")
    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(pq, "write_table", fail)
    with pytest.raises(OSError, match="disk full"):
        store.compact("TEST", "minute")
    assert len(store.read_candles("TEST", "minute")) == 2


def test_concurrent_stores_preserve_independent_batches(tmp_path):
    def write(index):
        frame = candles(n=1)
        frame["timestamp"] += pd.Timedelta(minutes=index)
        ParquetStore(tmp_path).write_candles(frame, "TEST", "minute")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(12)))
    assert len(ParquetStore(tmp_path).read_candles("TEST", "minute")) == 12


@pytest.mark.parametrize("key", ["../escape", "bad/name", "bad\\name", ".."])
def test_path_traversal_rejected(tmp_path, key):
    with pytest.raises(ValueError):
        ParquetStore(tmp_path).read_latest(key)


@pytest.mark.parametrize("column,value", [("close", float("nan")), ("volume", -1), ("high", 50)])
def test_invalid_candles_are_rejected_before_write(tmp_path, column, value):
    frame = candles()
    frame[column] = value
    with pytest.raises(ValueError):
        ParquetStore(tmp_path).write_candles(frame, "TEST", "minute")


def test_filters_do_not_resurrect_superseded_candles(tmp_path):
    store = ParquetStore(tmp_path)
    store.write_candles(candles(n=1, close=100), "TEST")
    store.write_candles(candles(n=1, close=105), "TEST")
    assert store.read_candles("TEST", filters=[("close", "==", 100)]).empty


def test_naive_legacy_strings_are_read_as_ist_wall_clock(tmp_path):
    # The old pipeline stored naive IST strings; current writes are aware UTC.
    store = ParquetStore(tmp_path)
    path = tmp_path / "candles" / "symbol=TEST" / "interval=minute"
    path.mkdir(parents=True)
    legacy = candles()
    legacy["timestamp"] = ["2026-10-19T09:15:00", "2026-10-19T09:16:00"]
    pq.write_table(pa.Table.from_pandas(legacy), path / "data.parquet")
    # Legacy files predate current writes; an equal mtime would make file order a coin toss.
    os.utime(path / "data.parquet", (1_700_000_000, 1_700_000_000))
    # The same 09:16 IST candle re-written by the current pipeline must not duplicate.
    store.write_candles(candles("2026-10-19T03:46:00Z", n=1, close=105), "TEST", "minute")
    result = store.read_candles("TEST", "minute")
    assert result["timestamp"].tolist() == [pd.Timestamp("2026-10-19T03:45:00Z"), pd.Timestamp("2026-10-19T03:46:00Z")]
    assert result["close"].tolist() == [100, 105]


def _uncached(tmp_path, symbol="TEST", interval="minute"):
    return ParquetStore(tmp_path, cache_rows=0).read_candles(symbol, interval)


def test_cached_reads_open_only_new_files_and_match_an_uncached_read(tmp_path, monkeypatch):
    store, writer = ParquetStore(tmp_path), ParquetStore(tmp_path, cache_rows=0)  # writer: another process
    store.write_candles(candles(n=3), "TEST", "minute")
    store.read_candles("TEST", "minute")
    opened = []
    real = pq.ParquetFile
    monkeypatch.setattr(pq, "ParquetFile", lambda path: opened.append(path) or real(path))
    writer.write_candles(candles("2026-10-19T03:48:00Z"), "TEST", "minute")
    writer.write_candles(candles(n=1, close=105), "TEST", "minute")  # a later retry of the first key wins
    actual = store.read_candles("TEST", "minute")
    assert len(opened) == 2
    assert actual["close"].tolist() == [105, 100, 100, 100, 100]
    pd.testing.assert_frame_equal(actual, _uncached(tmp_path))
    opened.clear()
    pd.testing.assert_frame_equal(store.read_latest("TEST", "minute", 2), actual.tail(2).reset_index(drop=True))
    assert opened == []


def test_a_file_committed_out_of_order_or_a_compaction_rereads_in_full(tmp_path):
    store, other = ParquetStore(tmp_path), ParquetStore(tmp_path, cache_rows=0)
    store.write_candles(candles(n=2), "TEST", "minute")
    store.read_candles("TEST", "minute")
    other.write_candles(candles(n=1, close=95), "TEST", "minute")
    newest = max((tmp_path / "candles").rglob("*.parquet"), key=lambda p: p.stat().st_mtime_ns)
    os.utime(newest, ns=(1, 1))  # an older commit time: it must lose to the cached file's candle
    assert store.read_candles("TEST", "minute")["close"].tolist() == [100, 100]
    pd.testing.assert_frame_equal(store.read_candles("TEST", "minute"), _uncached(tmp_path))
    other.write_candles(candles("2026-10-19T03:50:00Z", n=1), "TEST", "minute")
    other.compact("TEST", "minute")
    pd.testing.assert_frame_equal(store.read_candles("TEST", "minute"), _uncached(tmp_path))
    other.delete_symbol("TEST")
    assert store.read_candles("TEST", "minute").empty and store.read_latest("TEST", "minute").empty


def test_cached_frames_are_not_shared_with_callers_and_rows_are_bounded(tmp_path):
    store = ParquetStore(tmp_path, cache_rows=5)
    store.write_candles(candles(n=3), "A", "minute")
    store.write_candles(candles(n=3), "B", "minute")
    first = store.read_candles("A", "minute")
    first.loc[0, "close"] = -1
    latest = store.read_latest("A", "minute", 3)
    latest.loc[0, "close"] = -1
    assert store.read_candles("A", "minute")["close"].tolist() == [100, 100, 100]
    store.read_candles("B", "minute")
    assert list(store._cache) == [("B", "minute")] and store._cached_rows == 3
    store.write_candles(candles(n=6), "C", "minute")
    store.read_candles("C", "minute")  # larger than the whole budget: read, never cached
    assert list(store._cache) == [("B", "minute")] and store._cached_rows == 3


def test_compaction_removes_the_partitions_it_empties(tmp_path):
    store = ParquetStore(tmp_path)
    store.write_candles(candles(), "TEST", "minute", partition_by=["year", "month", "day"])
    store.write_candles(candles("2026-10-20T03:45:00Z"), "TEST", "minute", partition_by=["year", "month", "day"])
    root = tmp_path / "candles" / "symbol=TEST" / "interval=minute"
    assert store.compact("TEST", "minute") == 4
    assert [p.name for p in root.iterdir()] == [next(root.glob("*.parquet")).name]
    assert len(store.read_candles("TEST", "minute")) == 4


def test_nightly_compaction_merges_only_fragmented_intervals(tmp_path, monkeypatch):
    from trading_agent.scripts import scheduler
    from trading_agent.storage import parquet_store as parquet_module

    store = ParquetStore(tmp_path)
    monkeypatch.setattr(parquet_module, "_store", store)
    for minute in range(4):
        store.write_candles(candles(f"2026-10-19T03:{45 + minute}:00Z", n=1), "MANY", "5minute")
    store.write_candles(candles(), "FEW", "5minute")
    store.write_candles(candles("2026-10-19T03:50:00Z"), "FEW", "5minute")
    scheduler.compact_store_cycle(min_files=2)
    assert store.file_count("MANY", "5minute") == 1 and len(store.read_candles("MANY", "5minute")) == 4
    assert store.file_count("FEW", "5minute") == 2
