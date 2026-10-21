from threading import Event
from types import SimpleNamespace
from time import monotonic, sleep
from unittest.mock import Mock

from trading_agent.analysis.storage_coverage import StorageCoverage


def completed(cache, store, symbols, interval):
    deadline = monotonic() + 3
    while monotonic() < deadline:
        result = cache.snapshot(store, symbols, interval)
        if result["status"] != "pending":
            return result
        sleep(.005)
    raise AssertionError("Storage inventory did not finish")


def test_slow_inventory_does_not_block_requests_or_spawn_overlapping_workers(tmp_path):
    entered, release = Event(), Event()
    calls = []

    def symbols():
        calls.append(1)
        entered.set()
        release.wait(timeout=3)
        return ["AAA"]

    store = SimpleNamespace(root_path=tmp_path, get_symbols=symbols, get_intervals=lambda _: ["minute"])
    cache = StorageCoverage()
    try:
        assert cache.snapshot(store, ["AAA", "BBB"], "minute")["status"] == "pending"
        assert entered.wait(timeout=1)
        for _ in range(10):
            assert cache.snapshot(store, ["AAA", "BBB"], "minute")["stored_for_interval"] is None
        assert calls == [1]
    finally:
        release.set()
    assert completed(cache, store, ["AAA", "BBB"], "minute")["stored_for_interval"] == 1
    assert calls == [1]


def test_inventory_cache_binds_universe_interval_and_expiry(tmp_path):
    clock = [0]
    store = SimpleNamespace(root_path=tmp_path, get_symbols=Mock(return_value=["AAA"]),
                            get_intervals=lambda _: ["minute"])
    cache = StorageCoverage(clock=lambda: clock[0])
    assert completed(cache, store, ["AAA"], "minute")["stored_for_interval"] == 1
    assert completed(cache, store, ["BBB"], "minute")["stored_for_interval"] == 0
    assert completed(cache, store, ["AAA"], "5minute")["stored_for_interval"] == 0
    clock[0] = 61
    store.get_symbols.return_value = []
    assert completed(cache, store, ["AAA"], "minute")["stored_for_interval"] == 0


def test_inventory_failure_is_visible_and_retried_without_leaking_paths(tmp_path):
    clock = [0]
    store = SimpleNamespace(root_path=tmp_path, get_symbols=Mock(side_effect=OSError("private path")))
    cache = StorageCoverage(clock=lambda: clock[0])
    result = completed(cache, store, ["AAA"], "minute")
    assert result == {"status": "error", "stored_for_interval": None, "observed_at": None}
    clock[0] = 61
    store.get_symbols.side_effect = None
    store.get_symbols.return_value = []
    assert completed(cache, store, ["AAA"], "minute")["status"] == "ready"
