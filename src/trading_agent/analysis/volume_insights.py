"""Volume-based intraday recommendations.

Relative volume (RVOL) compares today's cumulative volume with the average
cumulative volume at the SAME time of day over the previous sessions, so a busy
open is not mistaken for a surge. Together with session VWAP and liquidity it
answers three questions the price-only signals cannot:

- Is the move real? A directional bar on RVOL >= 1.5 with price on the right
  side of VWAP is volume-confirmed; the same move on RVOL < 0.7 is suspect.
- Can it be traded? Liquidity tiers from average daily traded value.
- How big? A position is capped at ``EXECUTION_MAX_PARTICIPATION_PCT`` of the
  stock's average daily traded value, so size never outgrows the market.

These are recommendations and filters, not a backtested signal.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from trading_agent.core.config import get_settings

IST = ZoneInfo("Asia/Kolkata")
LOOKBACK_SESSIONS = 20
CRORE = 1e7
TIERS = [(100 * CRORE, "high"), (10 * CRORE, "medium"), (1 * CRORE, "low")]


def liquidity_tier(avg_daily_value: float) -> str:
    for floor, name in TIERS:
        if avg_daily_value >= floor:
            return name
    return "illiquid"


def volume_profile(frame: pd.DataFrame, symbol: str = "") -> dict | None:
    """Volume read for the latest session in ``frame`` (minute candles, UTC timestamps)."""
    if frame is None or len(frame) < 60:
        return None
    df = frame[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    local = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(IST)
    df["session"] = local.dt.date
    df["minute"] = local.dt.hour * 60 + local.dt.minute
    sessions = sorted(df["session"].unique())
    today = df[df["session"] == sessions[-1]]
    prior = df[df["session"].isin(sessions[-1 - LOOKBACK_SESSIONS:-1])]
    if prior.empty or today.empty:
        return None

    now_minute = int(today["minute"].iloc[-1])
    today_cum = float(today["volume"].sum())
    prior_cum = prior[prior["minute"] <= now_minute].groupby("session")["volume"].sum()
    rvol = today_cum / prior_cum.mean() if prior_cum.mean() > 0 else None

    last5 = float(today["volume"].tail(5).sum())
    window = prior[(prior["minute"] > now_minute - 5) & (prior["minute"] <= now_minute)]
    typical5 = window.groupby("session")["volume"].sum().mean()
    surge = last5 / typical5 if typical5 and typical5 > 0 else None

    typical_price = (today["high"] + today["low"] + today["close"]) / 3
    vwap = float((typical_price * today["volume"]).sum() / today_cum) if today_cum > 0 else None
    price = float(today["close"].iloc[-1])
    move_5 = float(price / today["close"].iloc[-6] - 1) if len(today) > 5 else 0.0

    daily_value = (prior["close"] * prior["volume"]).groupby(prior["session"]).sum()
    avg_daily_value = float(daily_value.mean())
    tier = liquidity_tier(avg_daily_value)
    participation = get_settings().execution.max_participation_pct
    max_value = participation * avg_daily_value

    vs_vwap = None if vwap is None else ("above" if price > vwap else "below" if price < vwap else "at")
    if tier == "illiquid":
        call, why = "AVOID", "Average daily traded value under INR 1 crore"
    elif rvol is None:
        call, why = "NEUTRAL", "No comparable prior-session volume"
    elif rvol < 0.7:
        call, why = "AVOID", f"Thin participation (RVOL {rvol:.2f}); moves are unreliable"
    elif rvol >= 1.5 and vs_vwap == "above" and move_5 > 0:
        call, why = "BUY_CONFIRMED", f"RVOL {rvol:.2f}, rising above session VWAP"
    elif rvol >= 1.5 and vs_vwap == "below" and move_5 < 0:
        call, why = "SELL_CONFIRMED", f"RVOL {rvol:.2f}, falling below session VWAP"
    elif rvol >= 1.5:
        call, why = "WATCH", f"Heavy volume (RVOL {rvol:.2f}) without a clear side of VWAP"
    else:
        call, why = "NEUTRAL", f"Normal volume (RVOL {rvol:.2f})"

    return {
        "symbol": symbol, "as_of": pd.Timestamp(df["timestamp"].iloc[-1]).isoformat(),
        "price": price, "rvol": None if rvol is None else round(rvol, 3),
        "bar_surge": None if surge is None else round(float(surge), 3),
        "vwap": None if vwap is None else round(vwap, 2), "price_vs_vwap": vs_vwap,
        "move_5_bars_pct": round(move_5 * 100, 3),
        "avg_daily_value_cr": round(avg_daily_value / CRORE, 2), "liquidity": tier,
        "max_position_value": round(max_value, 0),
        "max_quantity": int(max_value // price) if price > 0 else 0,
        "participation_pct": participation * 100,
        "sessions_compared": int(len(prior_cum)),
        "recommendation": call, "reason": why,
    }


def agreement(call: str, action: str | None) -> str:
    if action not in {"BUY", "SELL"} or call in {"NEUTRAL", "WATCH"}:
        return "n/a"
    if call == "AVOID":
        return "contradicts"
    return "confirms" if call.startswith(action) else "contradicts"


def symbol_volume(symbol: str, interval: str = "minute") -> dict | None:
    from trading_agent.analysis.research_engine import completed_frame
    from trading_agent.storage.parquet_store import get_parquet_store

    frame = completed_frame(get_parquet_store(), symbol, interval, datetime.now(UTC))
    return volume_profile(frame.tail(400 * (LOOKBACK_SESSIONS + 1)), symbol)


def scan_volume(rows: list[dict], top: int = 40) -> list[dict]:
    """Volume read for the strongest directional scan rows, highest RVOL first."""
    picks = sorted((r for r in rows if r.get("action") in {"BUY", "SELL"}), key=lambda r: -r["score"])[:top]
    out = []
    for row in picks:
        try:
            profile = symbol_volume(row["symbol"], row.get("interval", "minute"))
        except Exception:
            profile = None
        if profile:
            out.append({**profile, "scan_action": row["action"], "scan_score": row["score"],
                        "agreement": agreement(profile["recommendation"], row["action"])})
    return sorted(out, key=lambda r: -(r["rvol"] or 0))
