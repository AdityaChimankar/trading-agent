"""Session-based features for intraday patterns."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd

from trading_agent.core.logging_config import get_logger

logger = get_logger(__name__)

# NSE market hours in IST
MARKET_OPEN = 9 * 60 + 15  # 9:15 AM
MARKET_CLOSE = 15 * 60 + 30  # 3:30 PM
PRE_OPEN_START = 9 * 60  # 9:00 AM


def get_session_phase(timestamp: datetime) -> str:
    """Get trading session phase for a timestamp."""
    # Convert to IST
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    timestamp.astimezone(tz=None)  # Would need pytz for proper IST

    # Simplified - assuming timestamp is already in IST or we extract hour/minute
    minutes = timestamp.hour * 60 + timestamp.minute

    if minutes < PRE_OPEN_START:
        return "pre_market"
    elif minutes < MARKET_OPEN:
        return "pre_open"
    elif minutes < MARKET_OPEN + 60:
        return "opening"
    elif minutes < MARKET_OPEN + 120:
        return "morning"
    elif minutes < MARKET_OPEN + 300:  # ~2:30 PM
        return "midday"
    elif minutes < MARKET_CLOSE:
        return "closing"
    else:
        return "post_market"


def add_session_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add comprehensive session-based features."""
    df = df.copy()

    if "timestamp" not in df.columns:
        return df

    ts = pd.to_datetime(df["timestamp"])
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize(UTC)

    # Convert to IST (UTC+5:30)
    # Note: This is simplified - proper implementation would use pytz
    ist = ts + pd.Timedelta(hours=5, minutes=30)

    df["ist_hour"] = ist.dt.hour
    df["ist_minute"] = ist.dt.minute
    df["minutes_from_open"] = (ist.dt.hour * 60 + ist.dt.minute) - MARKET_OPEN
    df["minutes_to_close"] = MARKET_CLOSE - (ist.dt.hour * 60 + ist.dt.minute)

    # Session phase
    df["session_phase"] = df.apply(
        lambda row: get_session_phase(
            datetime(2024, 1, 1, int(row["ist_hour"]), int(row["ist_minute"]))
        ),
        axis=1,
    )

    # One-hot encode session phases
    phases = ["pre_market", "pre_open", "opening", "morning", "midday", "closing", "post_market"]
    for phase in phases:
        df[f"is_{phase}"] = (df["session_phase"] == phase).astype(int)

    # Time-based features
    df["is_first_hour"] = (df["minutes_from_open"] >= 0) & (df["minutes_from_open"] < 60)
    df["is_last_hour"] = (df["minutes_to_close"] > 0) & (df["minutes_to_close"] < 60)
    df["is_lunch"] = (df["ist_hour"] >= 12) & (df["ist_hour"] < 14)

    # Cyclical encoding
    df["tod_sin"] = np.sin(2 * np.pi * df["minutes_from_open"] / (MARKET_CLOSE - MARKET_OPEN))
    df["tod_cos"] = np.cos(2 * np.pi * df["minutes_from_open"] / (MARKET_CLOSE - MARKET_OPEN))

    # Day of week
    df["day_of_week"] = ist.dt.dayofweek
    df["is_monday"] = (df["day_of_week"] == 0).astype(int)
    df["is_friday"] = (df["day_of_week"] == 4).astype(int)

    # Expiry week (Thursday expiry)
    df["is_expiry_day"] = (df["day_of_week"] == 3).astype(int)
    df["days_to_expiry"] = (3 - df["day_of_week"]) % 7

    return df


def add_intraday_patterns(df: pd.DataFrame) -> pd.DataFrame:
    """Add intraday-specific patterns."""
    df = df.copy()

    if "timestamp" not in df.columns:
        return df

    # Volume patterns
    df["volume_ma_5"] = df["volume"].rolling(5).mean()
    df["volume_ma_20"] = df["volume"].rolling(20).mean()
    df["volume_ratio"] = df["volume"] / df["volume_ma_20"].replace(0, np.nan)

    # Price patterns relative to open
    if "open" in df.columns:
        first_open = df.groupby(df["timestamp"].dt.date)["open"].transform("first")
        df["pct_from_open"] = (df["close"] - first_open) / first_open * 100

    # VWAP distance
    if "vwap" in df.columns:
        df["vwap_dist_pct"] = (df["close"] - df["vwap"]) / df["vwap"] * 100

    # Opening range
    opening_mask = (
        df["is_opening_hour"] if "is_opening_hour" in df.columns else (df["minutes_from_open"] < 60)
    )
    if opening_mask.any():
        opening_high = (
            df.loc[opening_mask].groupby(df["timestamp"].dt.date)["high"].transform("max")
        )
        opening_low = df.loc[opening_mask].groupby(df["timestamp"].dt.date)["low"].transform("min")
        df["opening_range_high"] = opening_high
        df["opening_range_low"] = opening_low
        df["opening_range_size"] = (opening_high - opening_low) / opening_low * 100

        # Position within opening range
        df["or_position"] = (df["close"] - df["opening_range_low"]) / (
            df["opening_range_high"] - df["opening_range_low"]
        ).replace(0, np.nan)

    return df


def get_session_stats(df: pd.DataFrame) -> dict:
    """Get statistics by session phase."""
    if "session_phase" not in df.columns:
        return {}

    stats = {}
    for phase in df["session_phase"].unique():
        phase_data = df[df["session_phase"] == phase]
        stats[phase] = {
            "count": len(phase_data),
            "avg_return": phase_data["close"].pct_change().mean()
            if "close" in phase_data.columns
            else 0,
            "avg_volume": phase_data["volume"].mean() if "volume" in phase_data.columns else 0,
            "volatility": phase_data["close"].pct_change().std()
            if "close" in phase_data.columns
            else 0,
        }
    return stats
