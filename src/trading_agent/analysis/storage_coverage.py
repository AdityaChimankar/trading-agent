"""Single background directory inventory for responsive readiness requests."""
from datetime import UTC, datetime
from threading import Lock, Thread
from time import monotonic


class StorageCoverage:
    """Bound inventory work and cache metadata for at most one minute.

    A cold/stale request reports pending counts instead of blocking on thousands
    of bind-mounted directories. This never reads candles or validates signals.
    """

    def __init__(self, ttl=60, clock=monotonic):
        self.ttl, self.clock = ttl, clock
        self.lock = Lock()
        self.cached = {}
        self.running = False

    def snapshot(self, store, symbols, interval):
        symbols = frozenset(symbols)
        key = (str(store.root_path), symbols, interval)
        with self.lock:
            cached = self.cached.get(key)
            if cached and self.clock() - cached[0] < self.ttl:
                return dict(cached[1])
            if not self.running:
                self.running = True
                try:
                    Thread(target=self._refresh, args=(key, store, symbols, interval),
                           daemon=True, name="storage-coverage").start()
                except Exception:
                    self.running = False
                    raise
        return {"status": "pending", "stored_for_interval": None, "observed_at": None}

    def _refresh(self, key, store, symbols, interval):
        try:
            stored = set(store.get_symbols())
            present = sum(interval in store.get_intervals(symbol) for symbol in symbols & stored)
            result = {"status": "ready", "stored_for_interval": present,
                      "observed_at": datetime.now(UTC).isoformat()}
        except Exception:
            result = {"status": "error", "stored_for_interval": None, "observed_at": None}
        with self.lock:
            if len(self.cached) >= 4:
                self.cached.pop(next(iter(self.cached)))
            self.cached[key] = (self.clock(), result)
            self.running = False


_coverage = StorageCoverage()


def storage_coverage(store, symbols, interval):
    return _coverage.snapshot(store, symbols, interval)
