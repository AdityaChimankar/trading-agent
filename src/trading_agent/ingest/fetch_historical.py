"""Historical data fetching from Kite Connect."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from trading_agent.core.config import get_settings
from trading_agent.core.logging_config import get_logger
from trading_agent.core.exceptions import DataError, KiteAPIError, RateLimitError
from trading_agent.core.circuit_breaker import get_circuit_breaker
from trading_agent.ingest.kite_auth import get_kite_auth
from trading_agent.ingest.instrument_lookup import get_instrument_lookup
from trading_agent.storage.parquet_store import get_parquet_store

logger = get_logger(__name__)

# Kite historical data limits
MAX_DAYS_PER_REQUEST = {
    "minute": 60,
    "5minute": 100,
    "15minute": 200,
    "30minute": 200,
    "60minute": 400,
    "day": 2000,
}

INTERVAL_MAP = {
    "1minute": "minute",
    "5minute": "5minute",
    "15minute": "15minute",
    "30minute": "30minute",
    "60minute": "60minute",
    "1day": "day",
}


class HistoricalFetcher:
    """Fetch historical candles from Kite Connect."""

    def __init__(
        self,
        parquet_store: Path | None = None,
        compression: str = "snappy",
    ):
        self.auth = get_kite_auth()
        self.instruments = get_instrument_lookup()
        self.parquet = get_parquet_store(parquet_store, compression)
        self.circuit_breaker = get_circuit_breaker("kite_historical")

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type((KiteAPIError, RateLimitError, ConnectionError)),
    )
    def _fetch_chunk(
        self,
        token: int,
        interval: str,
        from_date: datetime,
        to_date: datetime,
    ) -> list[dict]:
        """Fetch a single chunk of historical data."""
        kite_interval = INTERVAL_MAP.get(interval, interval)

        def _call():
            return self.auth.kite.historical_data(
                instrument_token=token,
                from_date=from_date,
                to_date=to_date,
                interval=kite_interval,
                continuous=False,
                oi=False,
            )

        return self.circuit_breaker.call(_call)

    def fetch_symbol(
        self,
        symbol: str,
        interval: str = "5minute",
        days: int = 100,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        store: bool = True,
    ) -> pd.DataFrame:
        """Fetch historical data for a symbol.

        Args:
            symbol: Trading symbol
            interval: Candle interval
            days: Number of days to fetch (if from_date not specified)
            from_date: Start date (inclusive)
            to_date: End date (inclusive)
            store: Whether to store in Parquet

        Returns:
            DataFrame with candle data
        """
        token = self.instruments.get_token(symbol)
        if token is None:
            raise DataError(f"Symbol not found: {symbol}")

        # Determine date range
        if to_date is None:
            to_date = datetime.now(timezone.utc)
        if from_date is None:
            from_date = to_date - timedelta(days=days)

        # Ensure timezone-aware
        if from_date.tzinfo is None:
            from_date = from_date.replace(tzinfo=timezone.utc)
        if to_date.tzinfo is None:
            to_date = to_date.replace(tzinfo=timezone.utc)

        logger.info(
            "fetching_historical",
            symbol=symbol,
            interval=interval,
            from_date=from_date.date(),
            to_date=to_date.date(),
        )

        all_candles = []
        max_days = MAX_DAYS_PER_REQUEST.get(interval, 100)
        current_start = from_date

        while current_start < to_date:
            current_end = min(current_start + timedelta(days=max_days), to_date)

            try:
                candles = self._fetch_chunk(token, interval, current_start, current_end)
                all_candles.extend(candles)

                logger.debug(
                    "historical_chunk_fetched",
                    symbol=symbol,
                    from_date=current_start.date(),
                    to_date=current_end.date(),
                    count=len(candles),
                )

            except Exception as e:
                logger.error(
                    "historical_chunk_failed",
                    symbol=symbol,
                    from_date=current_start.date(),
                    to_date=current_end.date(),
                    error=str(e),
                )
                raise

            current_start = current_end + timedelta(seconds=1)
            time.sleep(0.1)  # Rate limiting

        if not all_candles:
            logger.warning("no_historical_data", symbol=symbol, interval=interval)
            return pd.DataFrame()

        # Convert to DataFrame
        df = pd.DataFrame(all_candles)
        df = df.rename(
            columns={
                "date": "timestamp",
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "volume": "volume",
            }
        )

        # Ensure timestamp is timezone-aware
        if df["timestamp"].dt.tz is None:
            df["timestamp"] = df["timestamp"].dt.tz_localize(timezone.utc)

        df = df.sort_values("timestamp").reset_index(drop=True)
        df.attrs["symbol"] = symbol

        # Store to Parquet
        if store:
            self.parquet.write_candles(df, symbol, interval)

        logger.info("historical_fetch_complete", symbol=symbol, interval=interval, rows=len(df))
        return df

    def fetch_watchlist(
        self,
        symbols: list[str],
        interval: str = "5minute",
        days: int = 100,
        parallel: bool = False,
        max_workers: int = 4,
    ) -> dict[str, pd.DataFrame]:
        """Fetch historical data for multiple symbols."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        results = {}

        if parallel and len(symbols) > 1:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(self.fetch_symbol, sym, interval, days): sym for sym in symbols
                }

                for future in as_completed(futures):
                    symbol = futures[future]
                    try:
                        results[symbol] = future.result()
                    except Exception as e:
                        logger.error("watchlist_fetch_failed", symbol=symbol, error=str(e))
                        results[symbol] = pd.DataFrame()
        else:
            for symbol in symbols:
                try:
                    results[symbol] = self.fetch_symbol(symbol, interval, days)
                except Exception as e:
                    logger.error("watchlist_fetch_failed", symbol=symbol, error=str(e))
                    results[symbol] = pd.DataFrame()

        return results

    def backfill(
        self,
        symbols: list[str],
        interval: str = "5minute",
        years: int = 2,
    ) -> dict[str, int]:
        """Backfill historical data for symbols.

        Returns:
            Dict of symbol -> rows fetched
        """
        to_date = datetime.now(timezone.utc)
        from_date = to_date - timedelta(days=years * 365)

        results = {}
        for symbol in symbols:
            try:
                df = self.fetch_symbol(symbol, interval, from_date=from_date, to_date=to_date)
                results[symbol] = len(df)
            except Exception as e:
                logger.error("backfill_failed", symbol=symbol, error=str(e))
                results[symbol] = 0

        return results


def fetch_historical(
    symbol: str,
    interval: str = "5minute",
    days: int = 100,
    **kwargs,
) -> pd.DataFrame:
    """Convenience function to fetch historical data."""
    fetcher = HistoricalFetcher()
    return fetcher.fetch_symbol(symbol, interval, days, **kwargs)
