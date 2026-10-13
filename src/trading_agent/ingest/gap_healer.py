"""Gap healing for missing candles in live feed."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import pandas as pd

from trading_agent.core.config import get_settings
from trading_agent.core.logging_config import get_logger
from trading_agent.core.freshness import get_freshness_monitor, FreshnessStatus
from trading_agent.ingest.instrument_lookup import get_instrument_lookup
from trading_agent.ingest.fetch_historical import HistoricalFetcher
from trading_agent.storage.parquet_store import get_parquet_store

logger = get_logger(__name__)


@dataclass
class Gap:
    """Represents a gap in candle data."""

    symbol: str
    interval: str
    expected_start: datetime
    expected_end: datetime
    missing_count: int


class GapHealer:
    """Detect and heal gaps in live candle data."""

    def __init__(
        self,
        symbols: list[str],
        interval: str = "5minute",
        check_interval_seconds: int = 60,
        max_gap_minutes: int = 60,
    ):
        self.symbols = symbols
        self.interval = interval
        self.check_interval = check_interval_seconds
        self.max_gap = timedelta(minutes=max_gap_minutes)

        self.fetcher = HistoricalFetcher()
        self.parquet = get_parquet_store()
        self.instruments = get_instrument_lookup()
        self.freshness = get_freshness_monitor()

        self._running = False
        self._task: asyncio.Task | None = None
        self._last_check: dict[str, datetime] = {}
        self._gap_callbacks: list[Callable[[Gap], None]] = []

    def _get_interval_seconds(self) -> int:
        interval_map = {
            "1minute": 60,
            "5minute": 300,
            "15minute": 900,
            "30minute": 1800,
            "60minute": 3600,
        }
        return interval_map.get(self.interval, 300)

    def _get_expected_candle_start(self, now: datetime) -> datetime:
        """Get the expected start time of the current candle."""
        interval_sec = self._get_interval_seconds()
        ts = int(now.timestamp())
        candle_ts = (ts // interval_sec) * interval_sec
        return datetime.fromtimestamp(candle_ts, tz=timezone.utc)

    async def check_gaps(self) -> list[Gap]:
        """Check for gaps in all symbols."""
        now = datetime.now(timezone.utc)
        current_candle_start = self._get_expected_candle_start(now)
        gaps = []

        for symbol in self.symbols:
            # Check freshness
            freshness = self.freshness.check(symbol)
            if freshness.status in (FreshnessStatus.CRITICAL, FreshnessStatus.UNKNOWN):
                # Try to backfill
                await self._heal_symbol(symbol, now)
                continue

            # Check for missing candles in Parquet
            last_candle = self.parquet.read_latest(symbol, self.interval, n=1)
            if last_candle.empty:
                # No data at all - backfill
                await self._heal_symbol(symbol, now)
                continue

            last_ts = last_candle["timestamp"].iloc[-1]
            expected_ts = current_candle_start - timedelta(seconds=self._get_interval_seconds())

            if last_ts < expected_ts:
                # Gap detected
                missing_seconds = (expected_ts - last_ts).total_seconds()
                interval_sec = self._get_interval_seconds()
                missing_count = int(missing_seconds / interval_sec)

                if (
                    missing_count > 0
                    and missing_count * interval_sec <= self.max_gap.total_seconds()
                ):
                    gap = Gap(
                        symbol=symbol,
                        interval=self.interval,
                        expected_start=last_ts + timedelta(seconds=interval_sec),
                        expected_end=expected_ts,
                        missing_count=missing_count,
                    )
                    gaps.append(gap)
                    logger.warning(
                        "gap_detected",
                        symbol=symbol,
                        missing=missing_count,
                        start=gap.expected_start,
                        end=gap.expected_end,
                    )

        return gaps

    async def _heal_symbol(self, symbol: str, to_date: datetime) -> int:
        """Heal gaps for a symbol by fetching missing data."""
        try:
            # Get earliest missing date
            last_candle = self.parquet.read_latest(symbol, self.interval, n=1)
            if last_candle.empty:
                from_date = to_date - timedelta(days=30)  # Default 30 days
            else:
                from_date = last_candle["timestamp"].iloc[-1] + timedelta(
                    seconds=self._get_interval_seconds()
                )

            if from_date >= to_date:
                return 0

            logger.info("healing_gap", symbol=symbol, from_date=from_date, to_date=to_date)
            df = self.fetcher.fetch_symbol(
                symbol,
                self.interval,
                from_date=from_date,
                to_date=to_date,
                store=True,
            )
            return len(df)
        except Exception as e:
            logger.error("gap_heal_failed", symbol=symbol, error=str(e))
            return 0

    async def heal_gaps(self, gaps: list[Gap]) -> dict[str, int]:
        """Heal detected gaps."""
        results = {}
        for gap in gaps:
            count = await self._heal_symbol(
                gap.symbol, gap.expected_end + timedelta(seconds=self._get_interval_seconds())
            )
            results[gap.symbol] = count

            # Trigger callbacks
            for callback in self._gap_callbacks:
                try:
                    callback(gap)
                except Exception as e:
                    logger.error("gap_callback_failed", error=str(e))

        return results

    def register_gap_callback(self, callback: Callable[[Gap], None]) -> None:
        """Register callback for gap detection."""
        self._gap_callbacks.append(callback)

    async def _monitor_loop(self) -> None:
        """Main monitoring loop."""
        while self._running:
            try:
                gaps = await self.check_gaps()
                if gaps:
                    await self.heal_gaps(gaps)
            except Exception as e:
                logger.error("gap_monitor_error", error=str(e))

            await asyncio.sleep(self.check_interval)

    async def start(self) -> None:
        """Start gap monitoring."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info("gap_healer_started", symbols=len(self.symbols), interval=self.interval)

    async def stop(self) -> None:
        """Stop gap monitoring."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("gap_healer_stopped")


# Convenience function
async def heal_gaps_for_symbols(
    symbols: list[str],
    interval: str = "5minute",
    max_gap_minutes: int = 60,
) -> dict[str, int]:
    """One-time gap healing for symbols."""
    healer = GapHealer(symbols, interval, max_gap_minutes=max_gap_minutes)
    gaps = await healer.check_gaps()
    return await healer.heal_gaps(gaps)
