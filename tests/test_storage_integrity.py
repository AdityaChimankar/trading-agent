"""Candle persistence invariants across live ingestion and historical repairs."""
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
    # The same 09:16 IST candle re-written by the current pipeline must not duplicate.
    store.write_candles(candles("2026-10-19T03:46:00Z", n=1, close=105), "TEST", "minute")
    result = store.read_candles("TEST", "minute")
    assert result["timestamp"].tolist() == [pd.Timestamp("2026-10-19T03:45:00Z"), pd.Timestamp("2026-10-19T03:46:00Z")]
    assert result["close"].tolist() == [100, 105]
