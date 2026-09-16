"""
Streams live ticks via KiteTicker (WebSocket) during market hours and
writes 1-minute aggregated candles to SQLite. Run this as a long-lived
process during 9:15 AM - 3:30 PM IST.

IMPORTANT (fixes repeated 1006 disconnects at scale): Kite's own
guidance is that the connection gets dropped if on_ticks is blocked
doing calculation/I-O - the server can't tell the client is still
alive if it's not consuming ticks fast enough. At hundreds of symbols
in MODE_FULL, opening a DB connection and committing inside on_ticks
(the old version of this file) can't keep up and gets disconnected
repeatedly. Fixed here by:
  1. on_ticks does ONLY fast in-memory dict updates, no DB access at all
  2. A separate background thread flushes completed minute-buckets to
     SQLite every few seconds, using one persistent connection
  3. MODE_QUOTE instead of MODE_FULL for large watchlists - we only
     ever use last_price/volume/ohlc, never order-book depth, so the
     heavier full-mode payload is wasted bandwidth at scale
"""
import threading
import time
from datetime import datetime
from collections import defaultdict
from kiteconnect import KiteTicker
from kite_auth import API_KEY
from db import get_connection, get_watchlist

FLUSH_INTERVAL_SEC = 5
# MODE_FULL is fine for a couple dozen symbols; MODE_QUOTE is lighter
# and all we actually use is last_price + volume_traded - switch
# automatically based on watchlist size so this doesn't need manual tuning.
LARGE_WATCHLIST_THRESHOLD = 100

with open(".access_token") as f:
    ACCESS_TOKEN = f.read().strip()

kws = KiteTicker(API_KEY, ACCESS_TOKEN)

_buckets = defaultdict(lambda: {"open": None, "high": None, "low": None, "close": None, "volume": 0, "minute": None})
_buckets_lock = threading.Lock()  # guards _buckets between the ticker thread and the flush thread

TOKEN_TO_SYMBOL = {w["instrument_token"]: w["symbol"] for w in get_watchlist()}
if not TOKEN_TO_SYMBOL:
    raise SystemExit(
        "Watchlist is empty. Run fetch_historical.py at least once first - "
        "it seeds the watchlist table this file reads from."
    )


def on_ticks(ws, ticks):
    """Kept intentionally minimal - in-memory dict updates only, no
    DB or other I/O. This is the part Kite's docs say must stay fast."""
    now_minute = datetime.now().strftime("%Y-%m-%dT%H:%M:00")
    with _buckets_lock:
        for tick in ticks:
            token = tick["instrument_token"]
            price = tick["last_price"]
            b = _buckets[token]

            if b["minute"] is not None and b["minute"] != now_minute:
                # minute rolled over - start a fresh bucket; the flush
                # thread picks up the completed one on its next pass
                _buckets[token] = {"open": price, "high": price, "low": price, "close": price, "volume": 0, "minute": now_minute}
                b = _buckets[token]
            elif b["minute"] is None:
                b["minute"] = now_minute
                b["open"] = price

            b["high"] = max(b["high"] or price, price)
            b["low"] = min(b["low"] or price, price)
            b["close"] = price
            b["volume"] = tick.get("volume_traded", b["volume"])


def flush_loop():
    """Background thread - the ONLY place that touches SQLite for
    live ticks. Runs on its own persistent connection, decoupled from
    the ticker callback entirely."""
    conn = get_connection()
    while True:
        time.sleep(FLUSH_INTERVAL_SEC)
        now_minute = datetime.now().strftime("%Y-%m-%dT%H:%M:00")

        with _buckets_lock:
            completed = [
                (TOKEN_TO_SYMBOL.get(token, str(token)), b["minute"], b["open"], b["high"], b["low"], b["close"], b["volume"])
                for token, b in _buckets.items()
                if b["minute"] is not None and b["minute"] != now_minute and b["open"] is not None
            ]

        if completed:
            conn.executemany(
                "INSERT OR REPLACE INTO candles (symbol, timestamp, open, high, low, close, volume) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", completed,
            )
            conn.commit()


def on_connect(ws, response):
    tokens = list(TOKEN_TO_SYMBOL.keys())
    ws.subscribe(tokens)
    mode = ws.MODE_QUOTE if len(tokens) > LARGE_WATCHLIST_THRESHOLD else ws.MODE_FULL
    ws.set_mode(mode, tokens)
    print(f"Subscribed to {len(tokens)} instruments in {'MODE_QUOTE' if mode == ws.MODE_QUOTE else 'MODE_FULL'}")


def on_close(ws, code, reason):
    print(f"Connection closed: {code} {reason}")


kws.on_ticks = on_ticks
kws.on_connect = on_connect
kws.on_close = on_close

if __name__ == "__main__":
    flush_thread = threading.Thread(target=flush_loop, daemon=True)
    flush_thread.start()
    kws.connect(threaded=False)
