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


# ---------------------------------------------------------------------------
# Causal session/regime feature bundle ("when NOT to trade" inputs) and the
# lookahead self-test. Every array at index i uses only candles <= i, so the
# same features are valid in research and live (no train/serve skew).
# ---------------------------------------------------------------------------

CACHE_KEY = "_session_features"
FEATURE_DEFAULTS = {
    "vwap_slope_bars": 12,       # bars used to judge whether session VWAP is rising
    "atr_pctile_window": 250,    # trailing window for the ATR percentile
    "atr_pctile_min_periods": 60,
    "volume_z_window": 20,
    "htf_fast_bars": 48,         # index-space higher-timeframe proxies
    "htf_slow_bars": 240,
    "forward_window": 12,        # hold horizon the expected-move term scales to
}


def _clock_parts(timestamps) -> tuple[np.ndarray, np.ndarray, float]:
    """(minute_of_day IST, day_index, bar_minutes). Unparseable minute -> -1."""
    from zoneinfo import ZoneInfo

    dt = pd.to_datetime(pd.Series(timestamps), format="mixed", utc=True, errors="coerce")
    ist = dt.dt.tz_convert(ZoneInfo("Asia/Kolkata"))
    minute = (ist.dt.hour * 60 + ist.dt.minute).astype("float64").fillna(-1.0).to_numpy()
    day = pd.factorize(ist.dt.strftime("%Y-%m-%d").fillna(""))[0].astype(np.int32)
    steps = dt.diff().dt.total_seconds().div(60)
    steps = steps[(steps > 0) & (steps <= 60)]
    bar_minutes = float(steps.median()) if len(steps) else 1.0
    return minute, day, bar_minutes


def _safe_ratio(num, den) -> np.ndarray:
    """NaN (never 0/inf) where undefined, so downstream gates decline to fire."""
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.asarray(num, dtype=float) / np.asarray(den, dtype=float)
    out[~np.isfinite(out)] = np.nan
    return out


def _group_cumsum(values: np.ndarray, groups: np.ndarray) -> np.ndarray:
    return pd.DataFrame({"g": groups, "v": values}).groupby("g", sort=False)["v"].cumsum().to_numpy(float)


def build(prepared: dict, params: dict | None = None) -> dict:
    """Every session/regime feature array for one symbol, aligned to prepared["length"]."""
    p = {**FEATURE_DEFAULTS, **(params or {})}
    n = prepared["length"]
    closes = np.asarray(prepared["closes"], dtype=float)
    opens = np.asarray(prepared["opens"], dtype=float)
    highs = np.asarray(prepared["highs"], dtype=float)
    lows = np.asarray(prepared["lows"], dtype=float)
    volumes = np.asarray(prepared["volumes"], dtype=float)
    atrs = np.asarray(prepared["atrs"], dtype=float)
    minute, day, bar_minutes = _clock_parts(prepared["timestamps"])

    bars_into_day = pd.DataFrame({"day": day}).groupby("day").cumcount().to_numpy(dtype=float)
    # From the CLOCK, never the day group's size: group size over the full
    # series would leak how many candles the rest of the session holds.
    bars_left = np.full(n, np.nan)
    known = minute >= 0
    bars_left[known] = np.maximum(0.0, np.ceil((MARKET_CLOSE - minute[known]) / bar_minutes))

    typical = (highs + lows + closes) / 3.0
    vwap = _safe_ratio(_group_cumsum(typical * volumes, day), _group_cumsum(volumes, day))
    dist_vwap = _safe_ratio(closes - vwap, vwap)
    slope_bars = int(p["vwap_slope_bars"])
    vwap_slope = np.full(n, np.nan)
    if n > slope_bars:  # explicit shift, never np.roll (which wraps the future in)
        vwap_slope[slope_bars:] = _safe_ratio(vwap[slope_bars:] - vwap[:-slope_bars], vwap[:-slope_bars])

    atr_pct = _safe_ratio(atrs, closes)
    atr_pctile = (pd.Series(atr_pct)
                  .rolling(p["atr_pctile_window"], min_periods=p["atr_pctile_min_periods"])
                  .rank(pct=True).to_numpy(dtype=float))

    w = p["volume_z_window"]
    v_ma = pd.Series(volumes).rolling(w, min_periods=w).mean().to_numpy()
    v_sd = pd.Series(volumes).rolling(w, min_periods=w).std(ddof=0).to_numpy()
    volume_z = _safe_ratio(volumes - v_ma, v_sd)

    htf = {}
    for name, bars in (("htf_fast_ret", p["htf_fast_bars"]), ("htf_slow_ret", p["htf_slow_bars"])):
        htf[name] = np.full(n, np.nan)
        if n > bars:
            htf[name][bars:] = closes[bars:] / closes[:-bars] - 1.0

    prev_close = np.r_[np.nan, closes[:-1]]
    gap = _safe_ratio(opens - prev_close, prev_close)
    gap[bars_into_day != 0] = np.nan  # an intraday move is not a gap

    return {
        "minute_of_day": minute, "day_index": day.astype(float),
        "bars_into_day": bars_into_day, "bars_left_in_day": bars_left,
        "vwap": vwap, "dist_vwap": dist_vwap, "vwap_slope": vwap_slope,
        "atr_pct": atr_pct, "atr_pctile": atr_pctile, "volume_z": volume_z,
        **htf, "gap_pct": gap,
        "expected_move": atr_pct * np.sqrt(p["forward_window"]),
    }


def features(prepared: dict, params: dict | None = None) -> dict:
    """build() memoized on the symbol-scoped ``prepared`` dict."""
    cached = prepared.get(CACHE_KEY)
    if cached is None:
        cached = prepared[CACHE_KEY] = build(prepared, params)
    return cached


def _prefix(prepared: dict, k: int) -> dict:
    short = {key: (value[:k] if isinstance(value, np.ndarray) else value)
             for key, value in prepared.items() if key != CACHE_KEY}
    short["length"] = k
    return short


def verify_causal(prepared: dict, params: dict | None = None, fraction: float = 0.7) -> tuple[bool, list[str]]:
    """Lookahead test: features over a truncated prefix must equal the full run's first k values.

    Any centered window, full-series normalization or bfill changes index i's
    value once later candles exist, so it fails here by construction.
    Returns (ok, mismatched_feature_names).
    """
    k = int(prepared["length"] * fraction)
    if k < 400:
        return True, []  # too little history for a meaningful comparison
    full = build(prepared, params)
    truncated = build(_prefix(prepared, k), params)
    mismatched = []
    for name, values in truncated.items():
        reference = full[name][:k]
        same = np.isclose(values, reference, rtol=1e-9, atol=1e-12) | (np.isnan(values) & np.isnan(reference))
        if not same.all():
            mismatched.append(name)
    return not mismatched, mismatched


def verify_sanity(prepared: dict, params: dict | None = None) -> list[str]:
    """Unit/scale checks a causally-correct but wrong feature would still pass."""
    problems = []
    bundle = build(prepared, params)
    lows = np.asarray(prepared["lows"], dtype=float)
    highs = np.asarray(prepared["highs"], dtype=float)
    session = bundle["minute_of_day"]
    in_session = (session >= MARKET_OPEN) & (session <= MARKET_CLOSE)
    if not in_session.any():
        problems.append("no candles inside NSE market hours 09:15-15:30 IST")
    vwap = bundle["vwap"]
    ok = np.isfinite(vwap)
    if ok.any() and (vwap[ok].min() < lows.min() * 0.99 or vwap[ok].max() > highs.max() * 1.01):
        problems.append("session VWAP falls outside the observed price range")
    pctile = bundle["atr_pctile"][np.isfinite(bundle["atr_pctile"])]
    if len(pctile) and (pctile.min() < 0 or pctile.max() > 1):
        problems.append("atr_pctile outside [0, 1]")
    for name in ("atr_pct", "expected_move", "dist_vwap"):
        if np.isinf(bundle[name]).any():
            problems.append(f"{name} contains inf")
    return problems
