"""Market screener: intraday filters, preset trading screens and sector breadth.

Modelled on the Groww "Explore" page (observed 2026-10-26), rebuilt from stored
minute candles instead of a vendor feed:

- Intraday screener: one row per stock with LTP, 1D change, change over the
  last 5/15/60 minutes, today's volume against the 1-week average, 52-week
  proximity, daily RSI(14), daily MACD(12,26,9) and previous-session floor
  pivots (R1-R3 / S1-S3). Filters combine with AND.
- Trading screens: named presets over those rows (R1 breakout, MACD above
  signal, RSI > 70, RSI < 30, ...), Groww's ``/stocks/screens/<id>`` pages.
- Sector breadth: advances/declines and the mean 1D change per NSE industry.

Groww ranks by its own "popularity" (turnover); this uses LTP x today's volume.
RSI, MACD, pivots and the 52-week range use completed sessions only, and the
1-week average volume the sessions of the previous 7 calendar days, as Groww's
do. ``rsi_live`` / ``macd_hist_live`` include the live bar. Checked against
Groww on 2026-10-26 (``screen_counts`` presets vs its screens): RSI within about
2 points, RSI > 70 / < 30 lists 100% / 95% recalled, 52-week range and previous
close exact for most names. Groww's R1 breakouts matched pivots from the session
before the previous one (90/95 recalled, vs 73/95 for the previous session), i.e.
its technicals lagged a session; this module keeps the standard previous-session
pivots. These are filters and context, not a backtested signal.
"""

from __future__ import annotations

import operator
from datetime import UTC, date, datetime, timedelta
from statistics import median
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from trading_agent.core.quant_indicators import macd, rsi

IST = ZoneInfo("Asia/Kolkata")
YEAR_SESSIONS = 250
HISTORY_DAYS = 400  # calendar days read per symbol: one year of sessions plus holidays
NEAR_LEVEL_PCT = 1.0  # "Near R1": within 1% below the level (Groww's threshold)
NEAR_52W_PCT = 5.0
MIN_RSI_SESSIONS = 15
MIN_MACD_SESSIONS = 35  # 26 for the slow EMA plus 9 for the signal line
STALE_LAG_MINUTES = 15  # behind the scan's newest bar by more than this = stale
SESSION_CLOSE = (15, 30)  # IST
CHANGE_WINDOWS = {"5m": 5, "15m": 15, "1h": 60}

OPS = {"eq": operator.eq, "ne": operator.ne, "gt": operator.gt, "ge": operator.ge,
       "lt": operator.lt, "le": operator.le, "in": lambda value, allowed: value in allowed,
       "has": lambda values, item: item in (values or [])}

# Groww's screens, plus the bearish mirror of the R1 breakout.
PRESETS = {
    "resistance-breakout": {
        "title": "Resistance breakouts", "bias": "bullish",
        "description": "Trading above the previous session's R1 floor pivot.",
        "filters": [("r1_breakout", "eq", True)]},
    "near-resistance": {
        "title": "Near resistance (R1)", "bias": "bullish",
        "description": f"Within {NEAR_LEVEL_PCT:g}% below R1; a break would be a breakout.",
        "filters": [("near_r1", "eq", True)]},
    "support-breakdown": {
        "title": "Support breakdowns", "bias": "bearish",
        "description": "Trading below the previous session's S1 floor pivot.",
        "filters": [("s1_breakdown", "eq", True)]},
    "macd-positive": {
        "title": "MACD positive", "bias": "bullish",
        "description": "Daily MACD(12,26,9) line above zero. Groww's 'MACD above signal line' screen is this "
                       "(93 of its 100 names on 2026-10-26, against 50 for a signal-line test).",
        "filters": [("macd", "gt", 0)]},
    "macd-above-signal": {
        "title": "MACD above signal line", "bias": "bullish",
        "description": "Daily MACD(12,26,9) line above its signal line.",
        "filters": [("macd_hist", "gt", 0)]},
    "macd-below-signal": {
        "title": "MACD below signal line", "bias": "bearish",
        "description": "Daily MACD(12,26,9) line at or below its signal line.",
        "filters": [("macd_hist", "le", 0)]},
    "rsi-overbought": {
        "title": "RSI overbought", "bias": "bearish",
        "description": "Daily RSI(14) at or above 70.",
        "filters": [("rsi", "ge", 70)]},
    "rsi-oversold": {
        "title": "RSI oversold", "bias": "bullish",
        "description": "Daily RSI(14) at or below 30.",
        "filters": [("rsi", "le", 30)]},
    "near-52w-high": {
        "title": "Near 52-week high", "bias": "bullish",
        "description": f"Within {NEAR_52W_PCT:g}% of the 52-week high.",
        "filters": [("year_high_proximity_pct", "le", NEAR_52W_PCT)]},
    "volume-shockers": {
        "title": "Volume shockers", "bias": "neutral",
        "description": "Today's volume already at least 2x the 1-week daily average.",
        "filters": [("volume_vs_week_pct", "ge", 100)]},
}
SORT_FIELDS = {"turnover", "ltp", "change_pct", "change_5m_pct", "change_15m_pct", "change_1h_pct",
               "volume", "volume_vs_week_pct", "rsi", "macd_hist", "year_high_proximity_pct", "symbol"}


IST_OFFSET_NS = 19_800 * 10**9  # +05:30, no DST
DAY_NS = 86_400 * 10**9


def _utc(values: pd.Series) -> pd.Series:
    return values if isinstance(values.dtype, pd.DatetimeTZDtype) else pd.to_datetime(values, utc=True)


def _session_keys(timestamps: pd.Series) -> np.ndarray:
    """IST calendar day of each timestamp as days since the epoch.

    Integer arithmetic instead of ``.dt.date``: building Python date objects was
    most of the scan's CPU time (0.27 s a symbol on 2026-10-26).
    """
    nanos = timestamps.dt.tz_convert("UTC").dt.tz_localize(None).to_numpy("datetime64[ns]").astype(np.int64)
    return (nanos + IST_OFFSET_NS) // DAY_NS


def daily_bars(frame: pd.DataFrame) -> pd.DataFrame:
    """One OHLCV row per IST session from intraday candles, oldest first, indexed by date."""
    frame = frame.assign(timestamp=_utc(frame["timestamp"])).sort_values("timestamp")
    grouped = frame.assign(session=_session_keys(frame["timestamp"])).groupby("session", sort=True)
    days = grouped.agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                       close=("close", "last"), volume=("volume", "sum"))
    days.index = pd.to_datetime(days.index.to_numpy() * DAY_NS).date
    return days


def _pct(new: float, old: float | None) -> float | None:
    return None if old is None or not old > 0 else round((new / old - 1) * 100, 3)


def _round(value, digits=2):
    return None if value is None or not np.isfinite(value) else round(float(value), digits)


def _daily_indicators(closes) -> tuple[float | None, tuple[float | None, float | None, float | None]]:
    """Last daily RSI(14) and MACD(12,26,9) line/signal/histogram, None when history is short."""
    rsi_value = float(rsi(closes, 14)[-1]) if len(closes) >= MIN_RSI_SESSIONS else None
    if len(closes) < MIN_MACD_SESSIONS:
        return rsi_value, (None, None, None)
    line, signal, histogram = macd(closes)
    return rsi_value, (float(line[-1]), float(signal[-1]), float(histogram[-1]))


def _apply_quote(days: pd.DataFrame, quote: dict) -> bool:
    """Put a live quote in as today's daily bar (appended, or replacing the stored partial one)."""
    session = quote["time"].astimezone(IST).date()
    if not quote.get("ltp", 0) > 0 or session < days.index[-1]:
        return False
    bar = [quote.get("open") or quote["ltp"], max(quote.get("high") or 0, quote["ltp"]),
           min(quote.get("low") or quote["ltp"], quote["ltp"]), quote["ltp"], quote.get("volume") or 0]
    days.loc[session] = bar
    return True


def screen_row(symbol: str, frame: pd.DataFrame, info: dict | None = None,
               quote: dict | None = None) -> dict | None:
    """Screener metrics for the latest session (intraday candles, UTC timestamps).

    ``quote`` (from ``ingest.live_quotes``) supplies today's price, OHLC, volume
    and the exchange's previous close when the stored candles lag behind; the
    daily history, RSI/MACD and pivots still come from the stored candles.
    """
    if frame is None or frame.empty:
        return None
    frame = frame.assign(timestamp=_utc(frame["timestamp"])).sort_values("timestamp")
    days = daily_bars(frame)
    quoted = bool(quote) and _apply_quote(days, quote)
    if len(days) < 2:
        return None
    if quoted and (quote.get("prev_close") or 0) > 0:
        # The exchange's official close (closing auction), not the last stored minute bar,
        # for the pivots and the completed-session RSI/MACD (SWIGGY 2026-10-23: 176.00 vs 177.80).
        days.iloc[-2, days.columns.get_loc("close")] = float(quote["prev_close"])
    session = days.index[-1]
    today, previous, completed = days.iloc[-1], days.iloc[-2], days.iloc[:-1]
    bar_time = frame["timestamp"].iloc[-1]
    ltp, last_time = (quote["ltp"], pd.Timestamp(quote["time"])) if quoted else (float(today["close"]), bar_time)
    if not ltp > 0:
        return None

    # Short-window changes need today's minute bars up to (about) the quote.
    keys = _session_keys(frame["timestamp"])
    intraday = frame[keys == keys[-1]]
    fresh_bars = (pd.Timestamp(bar_time).tz_convert(IST).date() == session
                  and (last_time - bar_time) <= timedelta(minutes=STALE_LAG_MINUTES))
    changes = {}
    for name, minutes in CHANGE_WINDOWS.items():
        earlier = intraday[intraday["timestamp"] <= last_time - timedelta(minutes=minutes)] if fresh_bars else intraday[:0]
        changes[f"change_{name}_pct"] = _pct(ltp, float(earlier["close"].iloc[-1])) if len(earlier) else None

    # Groww's conventions: the 52-week range and the 1-week average volume use
    # completed sessions only; the week is the previous 7 calendar days.
    year = completed.tail(YEAR_SESSIONS)
    year_high, year_low = float(year["high"].max()), float(year["low"].min())
    week = completed[[d >= session - timedelta(days=7) for d in completed.index]]["volume"]
    week_avg = float(week.mean()) if len(week) else None
    volume = float(today["volume"])

    live = days["close"].to_numpy(dtype=float)
    rsi_value, (macd_line, signal, hist) = _daily_indicators(live[:-1])
    rsi_live, (_, _, hist_live) = _daily_indicators(live)

    high, low, close = float(previous["high"]), float(previous["low"]), float(previous["close"])
    pivot = (high + low + close) / 3
    levels = {"pivot": pivot, "r1": 2 * pivot - low, "r2": pivot + (high - low), "r3": high + 2 * (pivot - low),
              "s1": 2 * pivot - high, "s2": pivot - (high - low), "s3": low - 2 * (high - pivot)}

    def near(level):  # within NEAR_LEVEL_PCT below a resistance
        return level > 0 and 0 <= (level - ltp) / level * 100 <= NEAR_LEVEL_PCT

    info = info or {}
    return {
        "symbol": symbol, "company": info.get("company") or None, "industry": info.get("industry"),
        "indices": info.get("indices", []), "session": session.isoformat(),
        "as_of": last_time.isoformat(), "price_source": "quote" if quoted else "candles",
        "bar_as_of": pd.Timestamp(bar_time).isoformat(), "ltp": round(ltp, 2), "prev_close": round(close, 2),
        "change": round(ltp - close, 2), "change_pct": _pct(ltp, close), **changes,
        "day_high": round(float(today["high"]), 2), "day_low": round(float(today["low"]), 2),
        "volume": int(volume), "volume_week_avg": None if week_avg is None else int(week_avg),
        "volume_vs_week_pct": _pct(volume, week_avg), "turnover": round(ltp * volume, 0),
        "year_high": round(year_high, 2), "year_low": round(year_low, 2),
        "year_high_proximity_pct": round((year_high - ltp) / year_high * 100, 3),
        "year_low_proximity_pct": round((ltp - year_low) / year_low * 100, 3),
        "new_52w_high": ltp > year_high, "new_52w_low": ltp < year_low,
        "history_sessions": int(len(year)), "full_year": len(completed) >= YEAR_SESSIONS,
        "rsi": _round(rsi_value), "macd": _round(macd_line, 4), "macd_signal": _round(signal, 4),
        "macd_hist": _round(hist, 4), "rsi_live": _round(rsi_live), "macd_hist_live": _round(hist_live, 4),
        **{k: round(v, 2) for k, v in levels.items()},
        "r1_breakout": ltp > levels["r1"], "r2_breakout": ltp > levels["r2"], "r3_breakout": ltp > levels["r3"],
        "near_r1": near(levels["r1"]), "near_r2": near(levels["r2"]), "near_r3": near(levels["r3"]),
        "s1_breakdown": ltp < levels["s1"],
    }


def mark_stale(rows: list[dict]) -> str | None:
    """Flag rows from an older session, or lagging the scan's newest bar (a ticker gap)."""
    if not rows:
        return None
    latest = max(row["session"] for row in rows)
    newest = max(datetime.fromisoformat(row["as_of"]) for row in rows)
    # After the close most quotes freeze at ~15:31 while a few keep updating; a price
    # from the closing minute is final, so lag is measured to 15:30 at the latest.
    day = date.fromisoformat(latest)
    newest = min(newest, datetime(day.year, day.month, day.day, *SESSION_CLOSE, tzinfo=IST))
    for row in rows:
        lag = max(0.0, (newest - datetime.fromisoformat(row["as_of"])).total_seconds() / 60)
        row["lag_minutes"] = round(lag, 1)
        row["stale"] = row["session"] < latest or lag > STALE_LAG_MINUTES
    return latest


def _matches(row: dict, field: str, op: str, value) -> bool:
    actual = row.get(field)
    if actual is None:
        return False
    return bool(OPS[op](actual, value))


def filter_rows(rows: list[dict], filters: list[tuple[str, str, object]] = (), *, sort: str = "turnover",
                descending: bool = True, limit: int | None = None, include_stale: bool = False) -> list[dict]:
    """Rows passing every filter, sorted; rows lacking a filtered field never match."""
    if sort not in SORT_FIELDS:
        raise ValueError(f"Unknown sort field {sort!r}")
    for _, op, _ in filters:
        if op not in OPS:
            raise ValueError(f"Unknown filter operator {op!r}")
    picked = [r for r in rows if (include_stale or not r.get("stale"))
              and all(_matches(r, *f) for f in filters)]
    present = [r for r in picked if r.get(sort) is not None]
    present.sort(key=lambda r: r[sort], reverse=descending)
    ordered = present + [r for r in picked if r.get(sort) is None]
    return ordered if limit is None else ordered[:limit]


def screen_counts(rows: list[dict]) -> list[dict]:
    """Every preset with how many current rows it matches."""
    return [{"id": key, "title": p["title"], "bias": p["bias"], "description": p["description"],
             "count": len(filter_rows(rows, p["filters"]))} for key, p in PRESETS.items()]


def sector_breadth(rows: list[dict], include_stale: bool = False) -> dict:
    """Advances, declines and the equal-weighted mean 1D change per NSE industry."""
    groups: dict[str, list[dict]] = {}
    unclassified = 0
    for row in rows:
        if row.get("change_pct") is None or (row.get("stale") and not include_stale):
            continue
        if not row.get("industry"):
            unclassified += 1
            continue
        groups.setdefault(row["industry"], []).append(row)
    sectors = []
    for name, members in groups.items():
        changes = [r["change_pct"] for r in members]
        best = max(members, key=lambda r: r["change_pct"])
        worst = min(members, key=lambda r: r["change_pct"])
        sectors.append({
            "sector": name, "stocks": len(members),
            "advances": sum(c > 0 for c in changes), "declines": sum(c < 0 for c in changes),
            "unchanged": sum(c == 0 for c in changes),
            "avg_change_pct": round(float(np.mean(changes)), 3),
            "median_change_pct": round(float(median(changes)), 3),
            "turnover": round(sum(r["turnover"] for r in members), 0),
            "top_gainer": {"symbol": best["symbol"], "change_pct": best["change_pct"]},
            "top_loser": {"symbol": worst["symbol"], "change_pct": worst["change_pct"]},
        })
    sectors.sort(key=lambda s: -s["avg_change_pct"])
    return {"sectors": sectors, "unclassified_stocks": unclassified,
            "method": "Equal-weighted mean of constituents' 1D change; NSE industry classification"}


def symbol_screen(symbol: str, info: dict | None = None, now: datetime | None = None,
                  quote: dict | None = None) -> dict | None:
    """Screener row for one stored symbol from its minute candles (and a live quote, if given)."""
    from trading_agent.storage.parquet_store import get_parquet_store

    now = now or datetime.now(UTC)
    frame = get_parquet_store().read_candles(symbol, "minute", start=now - timedelta(days=HISTORY_DAYS),
                                             columns=["timestamp", "open", "high", "low", "close", "volume"])
    # Drop the bar still forming, like the research scan's completed_frame.
    if not frame.empty:
        frame = frame[_utc(frame["timestamp"]) <= now - timedelta(minutes=1)]
    return screen_row(symbol, frame, info, quote)
