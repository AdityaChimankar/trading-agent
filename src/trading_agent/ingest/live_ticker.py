"""Live ticker using Kite Connect WebSocket."""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
import signal
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import math
from typing import Callable, Optional

import pandas as pd
from kiteconnect import KiteTicker

from trading_agent.core.config import get_settings
from trading_agent.core.logging_config import get_logger
from trading_agent.core.exceptions import KiteAPIError
from trading_agent.core.circuit_breaker import get_circuit_breaker
from trading_agent.ingest.kite_auth import get_kite_auth
from trading_agent.ingest.instrument_lookup import get_instrument_lookup
from trading_agent.ingest.heartbeat import write_heartbeat
from trading_agent.storage.parquet_store import get_parquet_store

logger = get_logger(__name__)


@dataclass
class TickData:
    """Single tick data."""

    symbol: str
    token: int
    timestamp: datetime
    last_price: float
    volume: int
    oi: int | None = None
    bid_price: float | None = None
    ask_price: float | None = None
    bid_qty: int | None = None
    ask_qty: int | None = None


@dataclass
class CandleData:
    """Aggregated candle data."""

    symbol: str
    interval: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    oi: int | None = None


class LiveTicker:
    """Kite Connect WebSocket ticker with candle aggregation."""

    def __init__(
        self,
        symbols: list[str],
        interval: str = "5minute",
        on_candle: Callable[[CandleData], None] | None = None,
        on_tick: Callable[[TickData], None] | None = None,
    ):
        self.symbols = symbols
        self.interval = interval
        self.on_candle = on_candle
        self.on_tick = on_tick

        self.auth = get_kite_auth()
        self.instruments = get_instrument_lookup()
        self.parquet = get_parquet_store()

        self._tokens: list[int] = []
        self._symbol_by_token: dict[int, str] = {}
        self._candle_buffers: dict[int, list[TickData]] = defaultdict(list)
        self._last_candle_time: dict[int, datetime] = {}
        self._last_volume: dict[int, tuple[object, int]] = {}
        self._last_tick: dict[int, datetime] = {}
        self._running = False
        self._ticker: KiteTicker | None = None
        self._last_heartbeat = 0.0

        # Circuit breaker for reconnection
        self._reconnect_cb = get_circuit_breaker("kite_ticker_reconnect")
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 10

    def _resolve_tokens(self) -> None:
        """Resolve symbols to instrument tokens."""
        self._tokens = []
        self._symbol_by_token = {}

        for symbol in self.symbols:
            token = self.instruments.get_token(symbol)
            if token:
                self._tokens.append(token)
                self._symbol_by_token[token] = symbol
            else:
                logger.warning("symbol_not_found", symbol=symbol)

        if not self._tokens:
            raise ValueError("No valid symbols to subscribe")

    def _get_interval_seconds(self) -> int:
        """Get interval in seconds."""
        interval_map = {
            "minute": 60,
            "1minute": 60,
            "5minute": 300,
            "15minute": 900,
            "30minute": 1800,
            "60minute": 3600,
        }
        return interval_map.get(self.interval, 300)

    def _get_candle_start(self, timestamp: datetime) -> datetime:
        """Get candle start timestamp for a given tick timestamp."""
        interval_sec = self._get_interval_seconds()
        ts = int(timestamp.timestamp())
        candle_ts = (ts // interval_sec) * interval_sec
        return datetime.fromtimestamp(candle_ts, tz=timezone.utc)

    def _aggregate_candle(self, token: int) -> CandleData | None:
        """Aggregate ticks into a candle."""
        ticks = self._candle_buffers.get(token, [])
        if not ticks:
            return None

        symbol = self._symbol_by_token[token]
        # Determine candle timestamp from first tick
        candle_start = self._get_candle_start(ticks[0].timestamp)

        # Aggregate
        prices = [t.last_price for t in ticks]
        volumes = [t.volume for t in ticks]

        candle = CandleData(
            symbol=symbol,
            interval=self.interval,
            timestamp=candle_start,
            open=prices[0],
            high=max(prices),
            low=min(prices),
            close=prices[-1],
            volume=sum(volumes),
            oi=ticks[-1].oi,
        )

        # Clear buffer for this token
        self._candle_buffers[token] = []
        self._last_candle_time[token] = candle_start

        return candle

    def _on_ticks(self, ws, ticks: list[dict]) -> None:
        """Handle incoming ticks."""
        for tick in ticks:
            token = tick["instrument_token"]
            if token not in self._symbol_by_token:
                continue

            symbol = self._symbol_by_token[token]
            # Kite full-mode messages carry datetime exchange_timestamp values.
            raw_timestamp = tick.get("exchange_timestamp", tick.get("timestamp"))
            if raw_timestamp is None:
                logger.warning("tick_missing_timestamp", symbol=symbol)
                continue
            if isinstance(raw_timestamp, datetime):
                timestamp = raw_timestamp
                if timestamp.tzinfo is None:
                    # SDK uses datetime.fromtimestamp(), which produces host
                    # local time (UTC in Docker, IST on many workstations).
                    timestamp = timestamp.astimezone()
                timestamp = timestamp.astimezone(timezone.utc)
            else:
                timestamp = datetime.fromtimestamp(raw_timestamp, tz=timezone.utc)
            if timestamp < self._last_tick.get(token, timestamp):
                logger.warning("out_of_order_tick", symbol=symbol)
                continue
            price = float(tick["last_price"])
            if not math.isfinite(price) or price <= 0:
                continue
            self._last_tick[token] = timestamp
            trading_day = timestamp.astimezone(ZoneInfo("Asia/Kolkata")).date()
            cumulative = max(0, int(tick.get("volume_traded", 0)))
            previous = self._last_volume.get(token)
            # On connection/reconnection we cannot attribute the day's prior
            # trades to this candle. Establish a baseline instead.
            volume = 0
            if previous and previous[0] == trading_day:
                volume = max(0, cumulative - previous[1])
            self._last_volume[token] = (trading_day, cumulative)
            depth = tick.get("depth") or {}
            buys = depth.get("buy") or [{}]
            sells = depth.get("sell") or [{}]

            # Create tick data
            tick_data = TickData(
                symbol=symbol,
                token=token,
                timestamp=timestamp,
                last_price=price,
                volume=volume,
                oi=tick.get("oi"),
                bid_price=buys[0].get("price"),
                ask_price=sells[0].get("price"),
            )

            # Callback for raw tick
            if self.on_tick:
                try:
                    self.on_tick(tick_data)
                except Exception as e:
                    logger.error("tick_callback_failed", error=str(e))

            # Emit the old candle BEFORE inserting the first tick of the new
            # interval. The first tick alone never completes a candle.
            candle_start = self._get_candle_start(timestamp)
            last_candle = self._last_candle_time.get(token)

            if last_candle is not None and candle_start > last_candle:
                # Candle completed, aggregate and emit
                candle = self._aggregate_candle(token)
                if candle and self.on_candle:
                    try:
                        self.on_candle(candle)
                    except Exception as e:
                        logger.error("candle_callback_failed", error=str(e))
            self._last_candle_time[token] = candle_start
            self._candle_buffers[token].append(tick_data)
        if self._last_tick and time.monotonic() - self._last_heartbeat >= 5:
            write_heartbeat(True, max(self._last_tick.values()), sum(map(len, self._candle_buffers.values())))
            self._last_heartbeat = time.monotonic()

    def _on_connect(self, ws, response) -> None:
        """Handle WebSocket connect."""
        logger.info("ticker_connected", tokens=len(self._tokens))
        write_heartbeat(True, max(self._last_tick.values(), default=None))
        self._last_volume.clear()
        ws.subscribe(self._tokens)
        ws.set_mode(ws.MODE_FULL, self._tokens)
        self._reconnect_attempts = 0

    def _on_close(self, ws, code, reason) -> None:
        """Handle WebSocket close."""
        logger.warning("ticker_disconnected", code=code, reason=reason)
        write_heartbeat(False, max(self._last_tick.values(), default=None))
        # KiteTicker owns reconnect/backoff on its reactor thread. Creating an
        # asyncio task here fails because this callback has no asyncio loop.

    def _on_error(self, ws, code, reason) -> None:
        """Handle WebSocket error."""
        logger.error("ticker_error", code=code, reason=reason)

    def _on_reconnect(self, ws, attempts) -> None:
        """Handle reconnection attempt."""
        logger.info("ticker_reconnecting", attempt=attempts)

    def _on_noreconnect(self, ws) -> None:
        """Handle max reconnection attempts reached."""
        logger.error("ticker_max_reconnect_reached")
        self._running = False

    def start(self) -> None:
        """Start the ticker."""
        if self._running:
            return

        self._resolve_tokens()
        self._running = True

        self._ticker = KiteTicker(
            api_key=self.auth.credentials.api_key,
            access_token=self.auth.credentials.access_token,
        )

        self._ticker.on_ticks = self._on_ticks
        self._ticker.on_connect = self._on_connect
        self._ticker.on_close = self._on_close
        self._ticker.on_error = self._on_error
        self._ticker.on_reconnect = self._on_reconnect
        self._ticker.on_noreconnect = self._on_noreconnect

        self._ticker.connect(threaded=True)

        logger.info("ticker_started", symbols=self.symbols, interval=self.interval)

    def stop(self) -> None:
        """Stop the ticker."""
        self._running = False
        write_heartbeat(False, max(self._last_tick.values(), default=None))
        if self._ticker:
            self._ticker.close()
        logger.info("ticker_stopped")

    def publish_heartbeat(self) -> None:
        """Report actual socket health even when no market ticks arrive.

        The command's main loop calls this independently of WebSocket callbacks.
        Last-tick timestamps remain exchange evidence, never the heartbeat time.
        """
        now = time.monotonic()
        if now - self._last_heartbeat < 15:
            return
        connected = bool(self._running and self._ticker and self._ticker.is_connected())
        last_tick = max(list(self._last_tick.values()), default=None)
        depth = sum(len(buffer) for buffer in list(self._candle_buffers.values()))
        write_heartbeat(connected, last_tick, depth)
        self._last_heartbeat = now

    def subscribe(self, symbols: list[str]) -> None:
        """Subscribe to additional symbols."""
        new_tokens = []
        for symbol in symbols:
            token = self.instruments.get_token(symbol)
            if token and token not in self._tokens:
                new_tokens.append(token)
                self._tokens.append(token)
                self._symbol_by_token[token] = symbol

        if new_tokens and self._ticker:
            self._ticker.subscribe(new_tokens)
            self._ticker.set_mode(self._ticker.MODE_FULL, new_tokens)

    def unsubscribe(self, symbols: list[str]) -> None:
        """Unsubscribe from symbols."""
        tokens_to_remove = []
        for symbol in symbols:
            token = self.instruments.get_token(symbol)
            if token and token in self._tokens:
                tokens_to_remove.append(token)

        if tokens_to_remove and self._ticker:
            self._ticker.unsubscribe(tokens_to_remove)
            self._tokens = [t for t in self._tokens if t not in tokens_to_remove]
            for t in tokens_to_remove:
                self._symbol_by_token.pop(t, None)
                self._candle_buffers.pop(t, None)
                self._last_candle_time.pop(t, None)


def create_default_candle_handler(parquet_store=None) -> Callable[[CandleData], None]:
    """Create a default candle handler that stores to Parquet."""
    store = parquet_store or get_parquet_store()

    def handler(candle: CandleData) -> None:
        df = pd.DataFrame(
            [
                {
                    "timestamp": candle.timestamp,
                    "open": candle.open,
                    "high": candle.high,
                    "low": candle.low,
                    "close": candle.close,
                    "volume": candle.volume,
                    "oi": candle.oi or 0,
                }
            ]
        )
        df.attrs["symbol"] = candle.symbol
        store.write_candles(df, candle.symbol, candle.interval)

    return handler


class BackgroundCandleWriter:
    """Run a candle handler on its own thread.

    KiteTicker calls on_ticks on its Twisted reactor thread. A blocking Parquet
    or DB write there delays WebSocket ping/pong and Kite drops the socket with
    code 1006, so the reactor only enqueues and this thread does the I/O.
    """

    _STOP = object()

    def __init__(self, handler: Callable[[CandleData], None], maxsize: int = 10_000):
        self.handler = handler
        self._queue: queue.Queue = queue.Queue(maxsize=maxsize)
        self._thread = threading.Thread(target=self._run, name="candle-writer", daemon=True)
        self._thread.start()

    def __call__(self, candle: CandleData) -> None:
        try:
            self._queue.put_nowait(candle)
        except queue.Full:
            logger.error("candle_writer_queue_full", symbol=candle.symbol)

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    def _run(self) -> None:
        while True:
            candle = self._queue.get()
            if candle is self._STOP:
                return
            try:
                self.handler(candle)
            except Exception as e:
                logger.error("candle_write_failed", symbol=candle.symbol, error=str(e))

    def close(self, timeout: float = 10.0) -> None:
        """Flush queued candles, then stop the thread."""
        self._queue.put(self._STOP)
        self._thread.join(timeout)


def main() -> None:
    """Start the live Kite ticker and block until it stops."""
    from trading_agent.core.logging_config import setup_logging
    from trading_agent.ingest.watchlist_resolved import get_watchlist_symbols

    setup_logging()
    auth = get_kite_auth()
    if not auth.credentials.access_token and not auth.load_access_token():
        raise SystemExit("No Kite access token. Run `trading-agent login` first.")

    symbols = get_watchlist_symbols()
    if not symbols:
        raise SystemExit("The watchlist is empty. Add symbols to symbols.txt.")

    writer = BackgroundCandleWriter(create_default_candle_handler())
    ticker = LiveTicker(symbols=symbols, interval=get_settings().storage.candle_interval,
                        on_candle=writer)
    def stop(signum, frame):
        raise SystemExit(0)

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        ticker.start()
        while ticker._running:
            ticker.publish_heartbeat()
            time.sleep(0.5)
    finally:
        ticker.stop()
        writer.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    main()
