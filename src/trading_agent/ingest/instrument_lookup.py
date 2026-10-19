"""Instrument lookup and symbol resolution."""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd

from trading_agent.core.config import get_settings
from trading_agent.core.exceptions import DataError
from trading_agent.core.logging_config import get_logger
from trading_agent.ingest.kite_auth import get_kite_auth

logger = get_logger(__name__)

class InstrumentLookup:
    """NSE instrument lookup and symbol resolution."""

    def __init__(self, cache_path: Path | None = None):
        self.cache_path = cache_path or get_settings().cache_dir / "instruments_nse.csv.gz"
        self._df: pd.DataFrame | None = None
        self._symbol_to_token: dict[str, int] = {}
        self._token_to_symbol: dict[int, str] = {}

    def load(self, force_refresh: bool = False) -> pd.DataFrame:
        """Load instruments from cache or download."""
        if self._df is not None and not force_refresh:
            return self._df

        if not force_refresh and self.cache_path.exists():
            # Check cache age
            age = time.time() - self.cache_path.stat().st_mtime
            if age < 86400:  # 24 hours
                try:
                    self._df = pd.read_csv(self.cache_path, compression="gzip")
                    self._build_indexes()
                    logger.info("instruments_loaded_from_cache", count=len(self._df))
                    return self._df
                except Exception as e:
                    logger.warning("instruments_cache_corrupt", error=str(e))

        # Download fresh
        self._df = self._download()
        self._build_indexes()
        self._save_cache()
        return self._df

    def _download(self) -> pd.DataFrame:
        """Download instruments from Kite."""
        auth = get_kite_auth()
        kite = auth.kite

        try:
            # Use Kite's instruments method
            instruments = kite.instruments("NSE")
            df = pd.DataFrame(instruments)
            logger.info("instruments_downloaded", count=len(df))
            return df
        except Exception as e:
            logger.error("instruments_download_failed", error=str(e))
            raise DataError(f"Failed to download instruments: {e}") from e

    def _build_indexes(self) -> None:
        """Build lookup indexes."""
        if self._df is None:
            return

        # Filter for equity only
        eq_df = self._df[self._df["segment"] == "NSE"]
        eq_df = eq_df[eq_df["instrument_type"] == "EQ"]

        self._symbol_to_token = dict(
            zip(eq_df["tradingsymbol"], eq_df["instrument_token"], strict=False)
        )
        self._token_to_symbol = dict(
            zip(eq_df["instrument_token"], eq_df["tradingsymbol"], strict=False)
        )

    def _save_cache(self) -> None:
        """Save instruments to cache."""
        if self._df is None:
            return

        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._df.to_csv(self.cache_path, index=False, compression="gzip")
        logger.info("instruments_cached", path=str(self.cache_path))

    def get_token(self, symbol: str) -> int | None:
        """Get instrument token for a symbol."""
        if not self._symbol_to_token:
            self.load()
        return self._symbol_to_token.get(symbol)

    def get_symbol(self, token: int) -> str | None:
        """Get symbol for an instrument token."""
        if not self._token_to_symbol:
            self.load()
        return self._token_to_symbol.get(token)

    def get_lot_size(self, symbol: str) -> int:
        """Get lot size for a symbol."""
        if self._df is None:
            self.load()

        row = self._df[self._df["tradingsymbol"] == symbol]
        if not row.empty:
            return int(row.iloc[0].get("lot_size", 1))
        return 1

    def get_exchange_token(self, symbol: str) -> int | None:
        """Get exchange token for a symbol."""
        if self._df is None:
            self.load()

        row = self._df[self._df["tradingsymbol"] == symbol]
        if not row.empty:
            return int(row.iloc[0].get("exchange_token", 0))
        return None

    def search(self, query: str, limit: int = 20) -> list[dict]:
        """Search symbols by name or symbol."""
        if self._df is None:
            self.load()

        eq_df = self._df[self._df["segment"] == "NSE"]
        eq_df = eq_df[eq_df["instrument_type"] == "EQ"]

        query = query.upper()
        mask = eq_df["tradingsymbol"].str.contains(query, na=False) | eq_df["name"].str.contains(
            query, na=False, case=False
        )
        results = eq_df[mask].head(limit)

        return results[["tradingsymbol", "name", "instrument_token", "lot_size"]].to_dict("records")

    def get_all_equity_symbols(self) -> list[str]:
        """Get all NSE equity symbols."""
        if not self._symbol_to_token:
            self.load()
        return sorted(self._symbol_to_token.keys())


# Global instance
_lookup: InstrumentLookup | None = None


def get_instrument_lookup(cache_path: Path | None = None) -> InstrumentLookup:
    """Get or create the global instrument lookup."""
    global _lookup
    if _lookup is None:
        _lookup = InstrumentLookup(cache_path)
    return _lookup
