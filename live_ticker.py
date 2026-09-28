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
_last_cumulative_volume: dict = {}  # token -> cumulative volume, absent until first seen

# Most recent minute the EXCHANGE has reported (from tick exchange_timestamp),
# or None before the first tick. The flush thread uses this to decide which
# buckets are complete. It deliberately does NOT use the machine's local
# clock: buckets are keyed by exchange time, so comparing them against a
# local wall clock only works if the machine is set to IST - otherwise
# buckets either never look complete (nothing is ever written) or look
# complete the instant they open (partial candles written).
_latest_exchange_minute = None

TOKEN_TO_SYMBOL = {w["instrument_token"]: w["symbol"] for w in get_watchlist()}
if not TOKEN_TO_SYMBOL:
    raise SystemExit(
        "Watchlist is empty. Run fetch_historical.py at least once first - "
        "it seeds the watchlist table this file reads from."
    )


def on_ticks(ws, ticks):
    """Kept intentionally minimal - in-memory dict updates only, no
    DB or other I/O. This is the part Kite's docs say must stay fast."""
    global _latest_exchange_minute
    with _buckets_lock:
        for tick in ticks:
            # Use exchange time if available (MODE_QUOTE/FULL), else fallback to local
            tick_time = tick.get("exchange_timestamp") or datetime.now()
            now_minute = tick_time.strftime("%Y-%m-%dT%H:%M:00")
            if _latest_exchange_minute is None or now_minute > _latest_exchange_minute:
                _latest_exchange_minute = now_minute

            token = tick["instrument_token"]
            price = tick["last_price"]
            b = _buckets[token]

            if b["minute"] is not None and b["minute"] != now_minute:
                _buckets[token] = {"open": price, "high": price, "low": price, "close": price, "volume": 0, "minute": now_minute}
                b = _buckets[token]
            elif b["minute"] is None:
                b["minute"] = now_minute
                b["open"] = price

            b["high"] = max(b["high"] or price, price)
            b["low"] = min(b["low"] or price, price)
            b["close"] = price

            cum_vol = tick.get("volume_traded", 0)
            if cum_vol > 0:
                # No baseline on first sight of a token, so record it
                # without inventing volume for this bar. (Using a missing
                # entry rather than 0 distinguishes "unseen" from a real
                # zero reading.)
                prev_vol = _last_cumulative_volume.get(token)
                delta = cum_vol - prev_vol if prev_vol is not None else 0
                b["volume"] += delta
                _last_cumulative_volume[token] = cum_vol


def flush_loop():
    """Background thread - the ONLY place that touches SQLite for
    live ticks. Runs on its own persistent connection, decoupled from
    the ticker callback entirely."""
    conn = get_connection()
    while True:
        time.sleep(FLUSH_INTERVAL_SEC)

        with _buckets_lock:
            latest_minute = _latest_exchange_minute
            if latest_minute is None:
                continue  # no ticks yet, so no bucket can be complete

            completed = []
            # A bucket is complete once the EXCHANGE has moved past its
            # minute. Drop each one as we flush it - otherwise a symbol
            # that stops ticking keeps its finished minute here forever
            # and gets re-inserted on every flush (INSERT OR REPLACE, so
            # harmless, but unbounded pointless work over a trading day).
            for token in [t for t, b in _buckets.items()
                          if b["minute"] is not None and b["minute"] < latest_minute
                          and b["open"] is not None]:
                b = _buckets.pop(token)
                completed.append((TOKEN_TO_SYMBOL.get(token, str(token)), b["minute"],
                                  b["open"], b["high"], b["low"], b["close"], b["volume"]))

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
