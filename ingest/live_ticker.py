"""
Streams live ticks via KiteTicker (WebSocket) during market hours and
writes 1-minute aggregated candles to SQLite. Run this as a long-lived
process during 9:15 AM - 3:30 PM IST.

Two problems drive the shape of this file.

**Keeping the socket healthy.** Kite's own guidance is that the connection
gets dropped if on_ticks is blocked doing calculation/I-O - the server can't
tell the client is still alive if it isn't consuming ticks fast enough. At
hundreds of symbols, opening a DB connection and committing inside on_ticks
can't keep up and gets disconnected repeatedly. So:
  1. on_ticks does ONLY fast in-memory dict updates, no DB access at all
  2. A separate background thread flushes completed minute-buckets to
     SQLite every few seconds, using one persistent connection
  3. MODE_QUOTE instead of MODE_FULL for large watchlists - we only ever use
     last_price/volume/ohlc, never order-book depth
  3a. A finished minute is HANDED OFF to a buffer for the flush thread, never
     overwritten in place. The old tick path replaced the bucket dict as soon
     as a tick from the next minute arrived, which silently discarded the
     completed candle unless the flush thread had already collected it -
     and since the flush interval is seconds while ticks arrive in
     milliseconds, that lost the candle for every symbol busy enough to
     matter. It showed up as coverage that was inversely correlated with
     liquidity: the most heavily traded names had the fewest candles.

**Keeping it live, and being honest when it isn't.** A healthy socket is not
the same thing as a live feed: the socket can stay open while no ticks
arrive (dropped subscriptions, an expired session, a network path that stops
delivering without closing), and the process can be killed and restarted
mid-session. Every one of those loses minutes of candles permanently, and
nothing downstream can tell the difference between "no data" and "nobody
traded". So this file also:
  4. Watches the tick stream itself and force-rehandshakes a socket that has
     gone quiet during market hours (silence is a dead feed, not a quiet
     market - 500 symbols never all stop at once)
  5. Hands the exact outage window to ingest/gap_healer.py, which refetches
     those minutes from Kite's historical endpoint
  6. Flushes the in-flight candle at the close and on Ctrl+C, because
     otherwise the final minute of the day is never written - nothing ever
     arrives to push the exchange clock past it
  7. Writes a heartbeat every few seconds so "live" vs "idle" is a fact
     anyone can read from the DB (see core/freshness.py, the /api endpoints)
"""
import queue
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta

from kiteconnect import KiteTicker

from core.freshness import MARKET_CLOSE, in_market_hours, session_start
from ingest.gap_healer import MINUTE_FMT, heal_window, last_completed_minute
from ingest.kite_auth import API_KEY
from paths import ACCESS_TOKEN_PATH
from storage.db import get_connection, get_watchlist


FLUSH_INTERVAL_SEC = 5
# MODE_FULL is fine for a couple dozen symbols; MODE_QUOTE is lighter
# and all we actually use is last_price + volume_traded - switch
# automatically based on watchlist size so this doesn't need manual tuning.
LARGE_WATCHLIST_THRESHOLD = 100
# No single tick for this long during market hours means the feed is not
# living up to its side of the bargain. 500 symbols do not all go quiet
# together, so this is a much safer signal than it looks.
STALL_WARN_SEC = 90
# Silence this long is treated as a dead feed: send a close frame so the
# client library re-handshakes and on_connect resubscribes.
FORCE_RECONNECT_SEC = 300
# Ride out a long outage rather than giving up - a gap we can heal afterwards
# is better than a process that stopped listening. 300 is Kite's ceiling.
RECONNECT_MAX_TRIES = 300
RECONNECT_MAX_DELAY_SEC = 30

with open(ACCESS_TOKEN_PATH) as f:
    ACCESS_TOKEN = f.read().strip()

# reconnect_max_delay/tries are passed explicitly: the library defaults
# (60s/50 attempts) give up after roughly an hour of a bad network, which for
# an intraday session means silently ending the day with no data.
kws = KiteTicker(
    API_KEY, ACCESS_TOKEN,
    reconnect=True,
    reconnect_max_tries=RECONNECT_MAX_TRIES,
    reconnect_max_delay=RECONNECT_MAX_DELAY_SEC,
)

_buckets = defaultdict(lambda: {"open": None, "high": None, "low": None, "close": None, "volume": 0, "minute": None})
# Finished candles handed over by on_ticks, waiting for the flush thread. A
# list, not a per-token slot: if the flusher ever falls behind, two completed
# minutes for the same symbol must both survive.
_completed: list = []
_buckets_lock = threading.Lock()  # guards _buckets and _completed between the ticker and flush threads
_last_cumulative_volume: dict = {}  # token -> cumulative volume, absent until first seen
_symbols_seen: set = set()          # tokens that have ticked since process start

# Most recent minute the EXCHANGE has reported (from tick exchange_timestamp),
# or None before the first tick. The flush thread uses this to decide which
# buckets are complete. It deliberately does NOT use the machine's local
# clock: buckets are keyed by exchange time, so comparing them against a
# local wall clock only works if the machine is set to IST - otherwise
# buckets either never look complete (nothing is ever written) or look
# complete the instant they open (partial candles written).
_latest_exchange_minute = None
_last_tick_monotonic = None   # monotonic clock of the newest tick, for the liveness watchdog
_stall_from = None            # first minute believed lost while the feed was silent

_connected = False
_connected_at = None
_disconnected_at = None
_reconnects = 0
_candles_written = 0
_reconnecting = False   # set while a forced rehandshake is in flight, so we don't retrigger

# Outage windows waiting to be refetched. A queue rather than direct calls so
# the flush thread never blocks on network I/O or the rate limit.
_heal_queue: "queue.Queue" = queue.Queue()

TOKEN_TO_SYMBOL = {w["instrument_token"]: w["symbol"] for w in get_watchlist()}
if not TOKEN_TO_SYMBOL:
    raise SystemExit(
        "Watchlist is empty. Run `python -m ingest.fetch_historical` at least once first - "
        "it seeds the watchlist table this file reads from."
    )


def _parse_minute(value: str) -> datetime:
    return datetime.strptime(value, MINUTE_FMT)


def on_ticks(ws, ticks):
    """Kept intentionally minimal - in-memory dict updates only, no DB or
    other I/O. This is the part Kite's docs say must stay fast."""
    global _latest_exchange_minute, _last_tick_monotonic
    _last_tick_monotonic = time.monotonic()
    with _buckets_lock:
        for tick in ticks:
            # Use exchange time if available (MODE_QUOTE/FULL), else fallback to local
            tick_time = tick.get("exchange_timestamp") or datetime.now()
            now_minute = tick_time.strftime(MINUTE_FMT)
            if _latest_exchange_minute is None or now_minute > _latest_exchange_minute:
                _latest_exchange_minute = now_minute

            token = tick["instrument_token"]
            _symbols_seen.add(token)
            price = tick["last_price"]
            b = _buckets[token]

            if b["minute"] is not None and b["minute"] != now_minute:
                # Hand the finished minute to the flusher. Replacing the dict
                # in place here (the old behaviour) dropped it on the floor.
                _completed.append((token, b))
                b = {"open": price, "high": price, "low": price, "close": price, "volume": 0, "minute": now_minute}
                _buckets[token] = b
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
                # zero reading.) Baselines are cleared on reconnect - the
                # new session reports its own cumulative counter, and
                # subtracting the old one would invent a huge volume spike.
                prev_vol = _last_cumulative_volume.get(token)
                delta = cum_vol - prev_vol if prev_vol is not None else 0
                b["volume"] += delta
                _last_cumulative_volume[token] = cum_vol


def _bucket_row(token, b):
    return (TOKEN_TO_SYMBOL.get(token, str(token)), b["minute"],
            b["open"], b["high"], b["low"], b["close"], b["volume"])


def _collect_buckets(force: bool):
    """Pop everything flushable out of the buffers. Caller must hold the lock.

    Handed-off candles are always written - they are finished minutes, and
    losing one is the bug this buffering exists to prevent. A still-open
    bucket is only written once the exchange has moved past its minute, or
    when `force` is set (session over), which is what stops the last minute
    of the day from being stranded in memory forever.
    """
    rows = []
    while _completed:
        token, b = _completed.pop()
        if b["open"] is not None:
            rows.append(_bucket_row(token, b))

    latest_minute = _latest_exchange_minute
    for token in list(_buckets):
        b = _buckets[token]
        if b["minute"] is None or b["open"] is None:
            continue
        complete = latest_minute is not None and b["minute"] < latest_minute
        if not (complete or force):
            continue
        del _buckets[token]
        rows.append(_bucket_row(token, b))
    return rows


def _write_candles(conn, rows) -> None:
    if not rows:
        return
    conn.executemany(
        "INSERT OR REPLACE INTO candles (symbol, timestamp, open, high, low, close, volume) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", rows,
    )
    conn.commit()


def _write_heartbeat(conn) -> None:
    """One small upsert every flush - the cheapest possible way to make the
    feed's liveness observable from outside this process."""
    with _buckets_lock:
        # Both buffers: an unflushed hand-off is data at risk, so it belongs
        # in the "how much is waiting" number.
        pending = len(_buckets) + len(_completed)
    conn.execute(
        """
        INSERT INTO ingest_status (id, connected, connected_at, disconnected_at, last_tick_at,
                                   symbols_seen, buckets_pending, candles_written,
                                   heal_queue_depth, reconnects, updated_at)
        VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            connected = excluded.connected,
            connected_at = excluded.connected_at,
            disconnected_at = excluded.disconnected_at,
            last_tick_at = excluded.last_tick_at,
            symbols_seen = excluded.symbols_seen,
            buckets_pending = excluded.buckets_pending,
            candles_written = excluded.candles_written,
            heal_queue_depth = excluded.heal_queue_depth,
            reconnects = excluded.reconnects,
            updated_at = excluded.updated_at
        """,
        (1 if _connected else 0, _connected_at, _disconnected_at, _latest_exchange_minute,
         len(_symbols_seen), pending, _candles_written, _heal_queue.qsize(), _reconnects,
         datetime.now().isoformat()),
    )
    conn.commit()


def _enqueue_heal(start: datetime, end: datetime) -> None:
    if start is None or end is None or end < start:
        return
    _heal_queue.put((start, end))
    print(f"[heal] queued outage {start.strftime(MINUTE_FMT)} .. {end.strftime(MINUTE_FMT)}")


def _discard_ended_buffered(suspect_minute: str) -> int:
    """Drop buffered candles for minutes in [suspect_minute, this minute).

    Caller must hold the lock. Used when the feed is known to have been
    interrupted: those bars were built from a subset of ticks, and a bar that
    has already been written can never be corrected (the healer only refills
    minutes that look *missing*), so they have to go before the healer sees
    the window.

    Only minutes that have already ENDED are dropped. The minute still in
    progress has no exchange record to refetch yet, so its partial is kept and
    left to finish filling from the reconnected stream - the best available
    answer, and strictly better than throwing away the ticks it already has.
    """
    current = datetime.now().strftime(MINUTE_FMT)
    dropped = [t for t, b in _buckets.items()
               if b["minute"] is not None and suspect_minute <= b["minute"] < current]
    for token in dropped:
        del _buckets[token]
    kept = [(t, b) for t, b in _completed
            if not (b["minute"] and suspect_minute <= b["minute"] < current)]
    lost = len(_completed) - len(kept)
    _completed[:] = kept
    return len(dropped) + lost


def _force_reconnect(reason: str) -> None:
    """Re-handshake a socket that is open but not delivering.

    Deliberately NOT kws.close(): that calls stop_retry(), which disables the
    client's auto-reconnect and would turn a recoverable stall into a dead
    process. Sending a close frame instead makes the factory treat it as a
    normal connection loss, so it retries and on_connect resubscribes.
    """
    global _reconnecting
    if _reconnecting:
        return
    _reconnecting = True
    print(f"[watchdog] {reason} - forcing a reconnect")
    try:
        _close_socket()
    except Exception as e:  # never let the watchdog kill the process
        print(f"[watchdog] could not force reconnect: {e}")
        _reconnecting = False


def _close_socket() -> None:
    if getattr(kws, "ws", None) is None:
        return
    try:
        kws._close(code=1000, reason="watchdog: feed silent")
    except AttributeError:
        # Older/newer client without _close: send the frame directly, which
        # has the same effect and still leaves the retry loop running.
        kws.ws.sendClose(1000, "watchdog: feed silent")


def flush_loop():
    """Background thread - the ONLY place that touches SQLite for live ticks
    and the only place that writes the heartbeat. Runs on its own persistent
    connection, decoupled from the ticker callback entirely.

    Every iteration is wrapped: this thread dying would be invisible until
    the session ended with most of its candles still in memory, which is the
    exact failure mode it exists to prevent.
    """
    conn = get_connection()
    while True:
        time.sleep(FLUSH_INTERVAL_SEC)
        try:
            _flush_once(conn)
        except Exception as e:
            print(f"[flush] iteration failed (continuing): {e}")


def _flush_once(conn):
    """One iteration: write what is ready, then decide what the silence (or
    its absence) means about the health of the feed."""
    global _stall_from, _candles_written, _reconnecting
    now = datetime.now()
    open_now = in_market_hours(now)
    silent_for = (time.monotonic() - _last_tick_monotonic) if _last_tick_monotonic else None
    stalled = silent_for is not None and silent_for > STALL_WARN_SEC and open_now
    # After the close nothing will ever arrive to advance the exchange clock,
    # so the buffer is forced out instead of waiting for a minute that never
    # ends. Deliberately NOT forced while merely stalled: a stalled in-flight
    # minute is incomplete, and writing it would leave the healer nothing to
    # repair (it only refills minutes that look missing).
    force = now.time() > MARKET_CLOSE or not open_now

    # --- flush ----------------------------------------------------------
    with _buckets_lock:
        rows = _collect_buckets(force=force)
        latest_minute = _latest_exchange_minute
    _write_candles(conn, rows)
    _candles_written += len(rows)

    with _buckets_lock:
        backlog = len(_completed)
    if backlog > 5000:
        print(f"[flush] WARNING: {backlog} completed candles are waiting to be "
              f"written - the flush thread is falling behind")

    # --- liveness watchdog ----------------------------------------------
    if stalled and _stall_from is None and latest_minute:
        # The feed died during this minute, so the minute itself is suspect -
        # not the one after it. Starting the repair here means the healer
        # refetches it from the exchange's record.
        _stall_from = _parse_minute(latest_minute)
        with _buckets_lock:
            dropped = _discard_ended_buffered(latest_minute)
        print(f"[watchdog] no tick for {int(silent_for)}s (last was {latest_minute}) - "
              f"discarded {dropped} partial candle(s) for repair")

    if open_now and silent_for is not None and silent_for > FORCE_RECONNECT_SEC:
        _force_reconnect(f"no tick for {int(silent_for)}s")

    if _stall_from is not None and silent_for is not None and silent_for < FLUSH_INTERVAL_SEC * 2:
        # Ticks are flowing again: the outage is over, so hand the window to
        # the healer and the missing minutes come back from the exchange's own
        # record instead of staying a permanent hole.
        _enqueue_heal(_stall_from, last_completed_minute(now))
        _stall_from = None
        if _reconnecting:
            print("[watchdog] feed is delivering again")

    # --- heartbeat ------------------------------------------------------
    _write_heartbeat(conn)


def heal_loop():
    """Drain outage windows, refetching each one from Kite's historical
    endpoint. Own thread so a rate-limited sweep never delays either the
    ticker callback or the flush loop."""
    while True:
        start, end = _heal_queue.get()
        conn = get_connection()
        try:
            from ingest.kite_auth import get_kite_client  # lazy: needs a live token, not an import-time one
            kite = get_kite_client()
            print(f"[heal] refetching {start.strftime(MINUTE_FMT)} .. {end.strftime(MINUTE_FMT)}")
            print(f"[heal] {heal_window(kite, conn, start, end)}")
        except Exception as e:
            print(f"[heal] sweep failed: {e}")
        finally:
            conn.close()
            _heal_queue.task_done()


def on_connect(ws, response):
    global _connected, _connected_at, _reconnects, _reconnecting
    was_reconnect = _connected_at is not None
    suspect = _latest_exchange_minute

    tokens = list(TOKEN_TO_SYMBOL.keys())
    ws.subscribe(tokens)
    mode = ws.MODE_QUOTE if len(tokens) > LARGE_WATCHLIST_THRESHOLD else ws.MODE_FULL
    ws.set_mode(mode, tokens)

    dropped = 0
    with _buckets_lock:
        if was_reconnect and suspect:
            # The tick volume counter is per trading session, so the baseline
            # kept across a drop would invent a huge delta on the next tick.
            _last_cumulative_volume.clear()
            dropped = _discard_ended_buffered(suspect)

    _connected = True
    _connected_at = datetime.now().isoformat()
    if was_reconnect:
        _reconnects += 1
    _reconnecting = False
    print(f"Subscribed to {len(tokens)} instruments in "
          f"{'MODE_QUOTE' if mode == ws.MODE_QUOTE else 'MODE_FULL'}"
          f"{f' (reconnect #{_reconnects}, {dropped} partial candle(s) held for repair)' if was_reconnect else ''}")

    if was_reconnect and suspect:
        # A drop shorter than the 90s watchdog threshold never gets flagged as
        # a stall, but it can still swallow whole minutes. Repair from the last
        # minute we actually saw; already-complete minutes cost nothing.
        _enqueue_heal(_parse_minute(suspect), last_completed_minute(datetime.now()))


def on_close(ws, code, reason):
    global _connected, _disconnected_at
    _connected = False
    _disconnected_at = datetime.now().isoformat()
    print(f"Connection closed: {code} {reason}")


def on_reconnect(ws, attempts_count):
    print(f"Reconnecting (attempt {attempts_count}/{RECONNECT_MAX_TRIES})...")


def on_noreconnect(ws):
    global _connected
    _connected = False
    # Loud on purpose: this is the end of live data for the day unless someone
    # acts, and it is exactly the failure that used to pass unnoticed.
    print(f"[ALERT] gave up reconnecting after {RECONNECT_MAX_TRIES} attempts. "
          f"Live data has stopped. Fix the connection and re-run "
          f"`python -m ingest.gap_healer` to fill the hole.")


def on_error(ws, code, reason):
    print(f"Connection error: {code} {reason}")


kws.on_ticks = on_ticks
kws.on_connect = on_connect
kws.on_close = on_close
kws.on_reconnect = on_reconnect
kws.on_noreconnect = on_noreconnect
kws.on_error = on_error


def _closing_sweep() -> None:
    """The definitive post-close repair: ask the healer to check the whole
    session and refill anything missing, including minutes lost to a restart
    that happened before this process existed."""
    try:
        from ingest.gap_healer import heal_day
        from ingest.kite_auth import get_kite_client
        print("[heal] end-of-session sweep...")
        print(f"[heal] {heal_day(get_kite_client(), get_connection())}")
    except Exception as e:
        print(f"[heal] end-of-session sweep failed: {e}")


if __name__ == "__main__":
    threading.Thread(target=flush_loop, daemon=True).start()
    threading.Thread(target=heal_loop, daemon=True).start()

    # A restart mid-session is itself an outage: check everything from the
    # session open up to the last closed minute. Symbols that are already
    # complete cost one indexed query each and no requests at all.
    now = datetime.now()
    if now.time() > MARKET_CLOSE or in_market_hours(now):
        _enqueue_heal(session_start(now), last_completed_minute(now))

    print("Live ticker starting - Ctrl+C at close (the buffer and the day's gaps are flushed on exit).")
    try:
        kws.connect(threaded=False)
    except KeyboardInterrupt:
        print("Interrupted - flushing the in-flight candle...")
    finally:
        with _buckets_lock:
            leftover = _collect_buckets(force=True)
        try:
            _write_candles(get_connection(), leftover)
            if leftover:
                print(f"Wrote {len(leftover)} buffered candle(s).")
        except Exception as e:
            print(f"Final flush failed: {e}")
        if datetime.now().time() > MARKET_CLOSE:
            _closing_sweep()
