"""Data ingestion module."""

from trading_agent.ingest.fetch_historical import HistoricalFetcher, fetch_historical
from trading_agent.ingest.fetch_news import NewsArticle, NewsFetcher, fetch_latest_news
from trading_agent.ingest.gap_healer import Gap, GapHealer, heal_gaps_for_symbols
from trading_agent.ingest.instrument_lookup import InstrumentLookup, get_instrument_lookup
from trading_agent.ingest.kite_auth import KiteAuth, KiteCredentials, get_kite_auth
from trading_agent.ingest.kite_auto_login import AutoLogin, ensure_valid_token
from trading_agent.ingest.live_ticker import (
    CandleData,
    LiveTicker,
    TickData,
    create_default_candle_handler,
)
from trading_agent.ingest.watchlist_resolved import (
    WatchlistManager,
    get_watchlist_symbols,
    resolve_watchlist,
)

__all__ = [
    # Kite Auth
    "KiteAuth",
    "KiteCredentials",
    "get_kite_auth",
    # Instruments
    "InstrumentLookup",
    "get_instrument_lookup",
    # Historical
    "HistoricalFetcher",
    "fetch_historical",
    # Live Ticker
    "LiveTicker",
    "TickData",
    "CandleData",
    "create_default_candle_handler",
    # Gap Healer
    "GapHealer",
    "Gap",
    "heal_gaps_for_symbols",
    # Watchlist
    "WatchlistManager",
    "get_watchlist_symbols",
    "resolve_watchlist",
    # Auto Login
    "AutoLogin",
    "ensure_valid_token",
    # News
    "NewsFetcher",
    "NewsArticle",
    "fetch_latest_news",
]
