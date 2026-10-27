"""Batched Kite quotes for the market screener.

The ticker streams only part of the universe, so on 2026-10-26 about 1,800 of
2,312 stored symbols had no minute bars after 09:18 and the screener showed
them as stale. One ``kite.quote`` call returns LTP, the day's OHLC, volume and
the previous close for up to 500 instruments, so the whole universe costs
about five calls. Used only by an explicit screener scan, never by a page load.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from trading_agent.core.logging_config import get_logger

logger = get_logger(__name__)
IST = ZoneInfo("Asia/Kolkata")
BATCH = 500  # Kite's limit per quote request
MIN_INTERVAL_SECONDS = 1.0  # Kite allows one quote request a second


def _aware(value, fallback: datetime) -> datetime:
    if not isinstance(value, datetime):
        return fallback
    # Kite returns naive exchange (IST) times.
    return value.replace(tzinfo=IST) if value.tzinfo is None else value


def parse_quote(raw: dict, fetched_at: datetime) -> dict | None:
    """One Kite quote entry as the screener's quote dict, or None without a price."""
    ltp = raw.get("last_price")
    if not isinstance(ltp, int | float) or not ltp > 0:
        return None
    ohlc = raw.get("ohlc") or {}
    return {"ltp": float(ltp), "open": ohlc.get("open") or None, "high": ohlc.get("high") or None,
            "low": ohlc.get("low") or None, "prev_close": ohlc.get("close") or None,
            "volume": int(raw.get("volume") or raw.get("volume_traded") or 0),
            "time": _aware(raw.get("timestamp"), fetched_at),
            "last_trade_time": _aware(raw.get("last_trade_time"), fetched_at)}


def fetch_quotes(symbols: list[str], kite=None, exchange: str = "NSE") -> dict[str, dict]:
    """Quotes keyed by symbol; symbols Kite does not return are simply absent."""
    if kite is None:
        from trading_agent.ingest.kite_auth import get_kite_client

        kite = get_kite_client()
    quotes: dict[str, dict] = {}
    last_call = 0.0
    for start in range(0, len(symbols), BATCH):
        batch = [f"{exchange}:{s}" for s in symbols[start:start + BATCH]]
        wait = MIN_INTERVAL_SECONDS - (time.monotonic() - last_call)
        if wait > 0:
            time.sleep(wait)
        last_call = time.monotonic()
        response = kite.quote(batch) or {}
        fetched_at = datetime.now(UTC)
        for key, raw in response.items():
            parsed = parse_quote(raw, fetched_at)
            if parsed is not None:
                quotes[key.split(":", 1)[-1]] = parsed
    logger.info("screener_quotes_fetched", requested=len(symbols), returned=len(quotes))
    return quotes
