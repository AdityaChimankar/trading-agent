"""Historical data fetching from Kite Connect."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo
from typing import Optional

import pandas as pd
from kiteconnect.exceptions import (
    InputException,
    KiteException,
    PermissionException,
    TokenException,
)
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception,
)

from trading_agent.core.config import get_settings
from trading_agent.core.logging_config import get_logger
from trading_agent.core.exceptions import (
    AuthenticationError,
    DataError,
    KiteAPIError,
    RateLimitError,
)
from trading_agent.core.circuit_breaker import CircuitBreakerConfig, get_circuit_breaker
from trading_agent.ingest.kite_auth import get_kite_auth
from trading_agent.ingest.instrument_lookup import get_instrument_lookup
from trading_agent.storage.parquet_store import get_parquet_store

logger = get_logger(__name__)

# Kite historical data limits
MAX_DAYS_PER_REQUEST = {
    "minute": 7,
    "1minute": 7,
    "5minute": 30,
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

# Keep historical traffic below the broker's burst threshold. This limiter is
# shared by all HistoricalFetcher instances in the process and also protects
# the parallel watchlist path.
HISTORICAL_REQUEST_INTERVAL_SECONDS = 0.4
_historical_request_lock = Lock()
_last_historical_request_at = 0.0


def _is_retryable_historical_error(error: BaseException) -> bool:
    """Return whether a failed historical request is safe to retry."""
    if isinstance(error, (RateLimitError, ConnectionError)):
        return True
    return (
        isinstance(error, KiteAPIError)
        and not isinstance(error, AuthenticationError)
        and (error.status_code is None or error.status_code >= 500)
    )


def _wait_for_historical_request_slot() -> None:
    """Serialize and pace historical API calls across the current process."""
    global _last_historical_request_at
    with _historical_request_lock:
        remaining = HISTORICAL_REQUEST_INTERVAL_SECONDS - (
            time.monotonic() - _last_historical_request_at
        )
        if remaining > 0:
            time.sleep(remaining)
        _last_historical_request_at = time.monotonic()


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
        self.circuit_breaker = get_circuit_breaker(
            "kite_historical",
            CircuitBreakerConfig(
                failure_threshold=5,
                timeout=30.0,
                excluded_exceptions=(
                    ValueError,
                    TypeError,
                    KeyError,
                    AuthenticationError,
                    DataError,
                ),
            ),
        )

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(3),
        retry=retry_if_exception(_is_retryable_historical_error),
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
            _wait_for_historical_request_slot()
            try:
                return self.auth.kite.historical_data(
                    instrument_token=token,
                    # The broker interprets date strings in exchange time.
                    # Passing an aware UTC datetime to the SDK drops its zone.
                    from_date=from_date.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S"),
                    to_date=to_date.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S"),
                    interval=kite_interval,
                    continuous=False,
                    oi=False,
                )
            except (TokenException, PermissionException) as exc:
                raise AuthenticationError(str(exc), status_code=exc.code) from exc
            except InputException as exc:
                raise DataError(f"Invalid historical-data request: {exc}") from exc
            except KiteException as exc:
                if exc.code == 429:
                    raise RateLimitError(str(exc)) from exc
                raise KiteAPIError(str(exc), status_code=exc.code) from exc

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

        # KiteConnect formats only exact ``datetime.datetime`` instances. A
        # pandas Timestamp otherwise passes through to the HTTP parameters and
        # the API rejects it as an invalid from/to date.
        if isinstance(from_date, pd.Timestamp):
            from_date = from_date.to_pydatetime()
        if isinstance(to_date, pd.Timestamp):
            to_date = to_date.to_pydatetime()

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

        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.drop_duplicates("timestamp", keep="last").sort_values("timestamp").reset_index(drop=True)
        interval_minutes = {"minute": 1, "1minute": 1, "3minute": 3, "5minute": 5,
                            "10minute": 10, "15minute": 15, "30minute": 30, "60minute": 60}
        if interval in interval_minutes:
            # Never expose a still-forming candle to strategies as final.
            cutoff = min(to_date, datetime.now(timezone.utc))
            df = df[df["timestamp"] + pd.Timedelta(minutes=interval_minutes[interval]) <= cutoff]
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
