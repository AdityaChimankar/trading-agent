"""Watchlist resolution and management."""

from __future__ import annotations

from pathlib import Path

from trading_agent.core.config import get_settings
from trading_agent.core.logging_config import get_logger
from trading_agent.ingest.instrument_lookup import get_instrument_lookup
from trading_agent.storage.db import WatchlistRepository, get_database

logger = get_logger(__name__)

# Default watchlist files
DEFAULT_WATCHLISTS = {
    "nifty50": "symbols_nifty50.txt",
    "nifty500": "symbols_nifty500.txt",
    "nse_all": "symbols_nse_all.txt",
}


class WatchlistManager:
    """Manage watchlist symbols from multiple sources."""

    def __init__(self, data_dir: Path | None = None):
        settings = get_settings()
        self.data_dir = data_dir or settings.data_dir
        self.db = get_database()
        self.instruments = get_instrument_lookup()

    def load_from_file(self, filename: str) -> list[str]:
        """Load symbols from a text file."""
        path = self.data_dir / filename
        if not path.exists():
            logger.warning("watchlist_file_not_found", file=str(path))
            return []

        symbols = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    symbols.append(line.upper())

        logger.info("watchlist_loaded", file=filename, count=len(symbols))
        return symbols

    def load_nifty50(self) -> list[str]:
        """Load Nifty 50 symbols."""
        return self.load_from_file(DEFAULT_WATCHLISTS["nifty50"])

    def load_nifty500(self) -> list[str]:
        """Load Nifty 500 symbols."""
        return self.load_from_file(DEFAULT_WATCHLISTS["nifty500"])

    def load_nse_all(self) -> list[str]:
        """Load all NSE symbols."""
        return self.load_from_file(DEFAULT_WATCHLISTS["nse_all"])

    def load_custom(self, name: str) -> list[str]:
        """Load custom watchlist by name."""
        return self.load_from_file(f"symbols_{name}.txt")

    def get_all_symbols(self) -> list[str]:
        """Get union of all known watchlists."""
        all_symbols = set()
        for name in DEFAULT_WATCHLISTS.values():
            all_symbols.update(self.load_from_file(name))
        return sorted(all_symbols)

    async def sync_to_db(self, symbols: list[str], source: str = "manual") -> int:
        """Sync symbols to database watchlist."""
        async with self.db.session() as session:
            repo = WatchlistRepository(session)
            count = 0
            for symbol in symbols:
                token = self.instruments.get_token(symbol)
                lot_size = self.instruments.get_lot_size(symbol)

                existing = await repo.get_by_symbol(symbol)
                if existing:
                    existing.instrument_token = token
                    existing.lot_size = lot_size
                    existing.is_active = True
                else:
                    await repo.create(
                        symbol=symbol,
                        instrument_token=token,
                        lot_size=lot_size,
                        is_active=True,
                    )
                count += 1

            await session.commit()
            logger.info("watchlist_synced", source=source, count=count)
            return count

    async def get_active_watchlist(self) -> list[str]:
        """Get active watchlist from database."""
        async with self.db.session() as session:
            repo = WatchlistRepository(session)
            watchlist = await repo.get_active()
            return [w.symbol for w in watchlist]

    async def resolve_watchlist(
        self,
        name: str | None = None,
        symbols: list[str] | None = None,
        use_db: bool = True,
    ) -> list[str]:
        """Resolve watchlist from multiple sources.

        Priority:
        1. Explicit symbols list
        2. Named watchlist file
        3. Database active watchlist
        4. Nifty 50 default
        """
        if symbols:
            return symbols

        if name:
            return self.load_custom(name)

        if use_db:
            db_symbols = await self.get_active_watchlist()
            if db_symbols:
                return db_symbols

        # Default to Nifty 50
        return self.load_nifty50()

    def validate_symbols(self, symbols: list[str]) -> tuple[list[str], list[str]]:
        """Validate symbols against instrument list.

        Returns:
            Tuple of (valid_symbols, invalid_symbols)
        """
        self.instruments.load()
        valid = []
        invalid = []
        for symbol in symbols:
            if self.instruments.get_token(symbol):
                valid.append(symbol)
            else:
                invalid.append(symbol)
        return valid, invalid

    def enrich_symbols(self, symbols: list[str]) -> list[dict]:
        """Enrich symbols with instrument metadata."""
        self.instruments.load()
        enriched = []
        for symbol in symbols:
            token = self.instruments.get_token(symbol)
            lot_size = self.instruments.get_lot_size(symbol)
            exchange_token = self.instruments.get_exchange_token(symbol)

            enriched.append(
                {
                    "symbol": symbol,
                    "instrument_token": token,
                    "lot_size": lot_size,
                    "exchange_token": exchange_token,
                }
            )
        return enriched


# Convenience functions
def get_watchlist_symbols(name: str | None = None) -> list[str]:
    """Get watchlist symbols (sync version)."""
    manager = WatchlistManager()
    if name:
        return manager.load_custom(name)
    return manager.load_nifty50()


async def resolve_watchlist(
    name: str | None = None,
    symbols: list[str] | None = None,
    use_db: bool = True,
) -> list[str]:
    """Resolve watchlist asynchronously."""
    manager = WatchlistManager()
    return await manager.resolve_watchlist(name, symbols, use_db)
