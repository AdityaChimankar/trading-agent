"""ML features for model training and inference."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC

import numpy as np
import pandas as pd

from trading_agent.core.logging_config import get_logger
from trading_agent.core.pattern_detection import compute_pattern_bias
from trading_agent.core.quant_indicators import (
    adx,
    atr,
    bollinger_bands,
    donchian_channels,
    ema,
    macd,
    rsi,
    stochastic,
    vwap,
)

logger = get_logger(__name__)


@dataclass(frozen=True)
class FeatureConfig:
    """Feature engineering configuration."""

    # Price features
    include_returns: bool = True
    include_log_returns: bool = True
    return_periods: list[int] = field(default_factory=lambda: [1, 3, 5, 10, 20])

    # Technical indicators
    include_rsi: bool = True
    rsi_periods: list[int] = field(default_factory=lambda: [14])
    include_macd: bool = True
    include_bb: bool = True
    include_stoch: bool = True
    include_donchian: bool = True
    include_vwap: bool = True

    # Trend features
    include_ema: bool = True
    ema_periods: list[int] = field(default_factory=lambda: [9, 21, 50])
    include_adx: bool = True
    adx_periods: list[int] = field(default_factory=lambda: [14])

    # Volatility
    include_atr: bool = True
    atr_periods: list[int] = field(default_factory=lambda: [14])
    include_bb_width: bool = True
    include_atr_pct: bool = True  # ATR as % of price

    # Pattern features
    include_patterns: bool = True
    swing_order: int = 5

    # Session/regime features
    include_session: bool = True
    include_time_features: bool = True

    # Lag features
    lag_periods: list[int] = field(default_factory=lambda: [1, 2, 3, 5])

    # Target
    target_horizon: int = 12  # 12 * 5min = 60min forward
    target_type: str = "return"  # "return", "direction", "triple_barrier"


DEFAULT_FEATURE_CONFIG = FeatureConfig()


def add_session_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add session-based features."""
    df = df.copy()
    if "timestamp" not in df.columns:
        return df

    ts = pd.to_datetime(df["timestamp"])
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize(UTC)

    # Convert to IST (UTC+5:30)
    ist = ts.dt.tz_convert("Asia/Kolkata")

    df["hour"] = ist.dt.hour
    df["minute"] = ist.dt.minute
    df["day_of_week"] = ist.dt.dayofweek  # 0=Monday

    # Session flags
    df["is_market_open"] = (
        (ist.dt.hour >= 9) & (ist.dt.hour < 15) | ((ist.dt.hour == 15) & (ist.dt.minute <= 30))
    ).astype(int)
    df["is_pre_market"] = ((ist.dt.hour == 9) & (ist.dt.minute < 15)).astype(int)
    df["is_opening_hour"] = (ist.dt.hour == 9).astype(int)
    df["is_closing_hour"] = (ist.dt.hour == 15).astype(int)
    df["is_lunch"] = ((ist.dt.hour >= 12) & (ist.dt.hour < 14)).astype(int)

    # Cyclical encoding
    df["hour_sin"] = np.sin(2 * np.pi * df["hour"] / 24)
    df["hour_cos"] = np.cos(2 * np.pi * df["hour"] / 24)
    df["minute_sin"] = np.sin(2 * np.pi * df["minute"] / 60)
    df["minute_cos"] = np.cos(2 * np.pi * df["minute"] / 60)
    df["dow_sin"] = np.sin(2 * np.pi * df["day_of_week"] / 5)
    df["dow_cos"] = np.cos(2 * np.pi * df["day_of_week"] / 5)

    return df


def add_return_features(df: pd.DataFrame, periods: list[int]) -> pd.DataFrame:
    """Add return features for multiple periods."""
    df = df.copy()
    close = df["close"]

    for p in periods:
        df[f"return_{p}"] = close.pct_change(p)
        df[f"log_return_{p}"] = np.log(close / close.shift(p))

    return df


def add_technical_features(df: pd.DataFrame, config: FeatureConfig) -> pd.DataFrame:
    """Add technical indicator features."""
    df = df.copy()

    df["open"].to_numpy()
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    c = df["close"].to_numpy()
    v = df["volume"].to_numpy()

    # RSI
    if config.include_rsi:
        for p in config.rsi_periods:
            df[f"rsi_{p}"] = rsi(c, p)

    # MACD
    if config.include_macd:
        macd_line, signal, hist = macd(c)
        df["macd"] = macd_line
        df["macd_signal"] = signal
        df["macd_hist"] = hist

    # Bollinger Bands
    if config.include_bb:
        upper, middle, lower = bollinger_bands(c, 20)
        df["bb_upper"] = upper
        df["bb_middle"] = middle
        df["bb_lower"] = lower
        if config.include_bb_width:
            df["bb_width"] = (upper - lower) / middle

    # Stochastic
    if config.include_stoch:
        k, d = stochastic(h, l, c)
        df["stoch_k"] = k
        df["stoch_d"] = d

    # Donchian
    if config.include_donchian:
        upper, middle, lower = donchian_channels(h, l, 20)
        df["donchian_upper"] = upper
        df["donchian_middle"] = middle
        df["donchian_lower"] = lower
        df["donchian_width"] = (upper - lower) / middle

    # VWAP
    if config.include_vwap:
        df["vwap"] = vwap(h, l, c, v)
        df["vwap_dist"] = (c - df["vwap"]) / df["vwap"]

    # EMAs
    if config.include_ema:
        for p in config.ema_periods:
            df[f"ema_{p}"] = ema(c, p)
            df[f"ema_{p}_dist"] = (c - df[f"ema_{p}"]) / df[f"ema_{p}"]

    # ADX
    if config.include_adx:
        for p in config.adx_periods:
            df[f"adx_{p}"] = adx(h, l, c, p)

    # ATR
    if config.include_atr:
        for p in config.atr_periods:
            atr_val = atr(h, l, c, p)
            df[f"atr_{p}"] = atr_val
            if config.include_atr_pct:
                df[f"atr_{p}_pct"] = atr_val / c * 100

    return df


def add_pattern_features(df: pd.DataFrame, config: FeatureConfig) -> pd.DataFrame:
    """Add candlestick pattern features."""
    if not config.include_patterns:
        return df

    df = df.copy()
    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    c = df["close"].to_numpy()

    pattern_bias = compute_pattern_bias(o, h, l, c, config.swing_order)
    df["pattern_bias"] = pattern_bias
    df["pattern_bullish"] = (pattern_bias == 1).astype(int)
    df["pattern_bearish"] = (pattern_bias == 2).astype(int)

    return df


def add_lag_features(df: pd.DataFrame, columns: list[str], periods: list[int]) -> pd.DataFrame:
    """Add lagged features.

    Uses pd.concat instead of iterative column insertion to avoid
    the PerformanceWarning about highly fragmented DataFrames.
    """
    lag_frames = []
    for col in columns:
        if col in df.columns:
            for p in periods:
                lag_frames.append(df[col].shift(p).rename(f"{col}_lag{p}"))
    if lag_frames:
        return pd.concat([df] + lag_frames, axis=1)
    return df


def create_target(
    df: pd.DataFrame,
    horizon: int,
    target_type: str = "return",
) -> pd.Series:
    """Create target variable."""
    close = df["close"]

    if target_type == "return":
        # Forward return
        target = close.shift(-horizon) / close - 1
    elif target_type == "direction":
        # Direction: 1=up, 0=down
        target = (close.shift(-horizon) > close).astype(int)
    elif target_type == "triple_barrier":
        # Triple barrier labeling (simplified)
        # Would need ATR for barriers
        target = close.shift(-horizon) / close - 1
    else:
        raise ValueError(f"Unknown target type: {target_type}")

    return target


def prepare_features(
    df: pd.DataFrame,
    config: FeatureConfig = DEFAULT_FEATURE_CONFIG,
) -> tuple[pd.DataFrame, pd.Series]:
    """Prepare feature matrix and target for ML.

    Returns:
        Tuple of (X, y) where X is features DataFrame and y is target Series.
    """
    df = df.copy()

    # Ensure sorted by timestamp
    if "timestamp" in df.columns:
        df = df.sort_values("timestamp").reset_index(drop=True)

    # Add all feature groups
    df = add_return_features(df, config.return_periods)
    df = add_technical_features(df, config)
    df = add_pattern_features(df, config)
    df = add_session_features(df)

    # Create target
    y = create_target(df, config.target_horizon, config.target_type)

    # Select feature columns (exclude target, timestamp, raw OHLCV)
    exclude_cols = {"timestamp", "open", "high", "low", "close", "volume", "oi", "target"}
    feature_cols = [c for c in df.columns if c not in exclude_cols and not c.startswith("target")]

    X = df[feature_cols].copy()

    # Add lag features for key indicators
    key_indicators = [
        c
        for c in feature_cols
        if any(
            c.startswith(prefix)
            for prefix in [
                "rsi_",
                "macd",
                "adx_",
                "atr_",
                "ema_",
                "bb_",
                "stoch",
                "donchian",
                "vwap",
                "return_",
            ]
        )
    ]
    X = add_lag_features(X, key_indicators, config.lag_periods)

    # Drop rows with NaN (from indicators and target)
    valid_mask = X.notna().all(axis=1) & y.notna()
    X = X[valid_mask]
    y = y[valid_mask]

    logger.info("features_prepared", n_samples=len(X), n_features=len(X.columns))
    return X, y


def get_feature_names(config: FeatureConfig = DEFAULT_FEATURE_CONFIG) -> list[str]:
    """Get list of feature names for a config (without preparing data)."""
    # This is a simplified version - in practice, prepare a small sample
    sample_df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=100, freq="5min", tz="UTC"),
            "open": np.random.randn(100).cumsum() + 100,
            "high": np.random.randn(100).cumsum() + 101,
            "low": np.random.randn(100).cumsum() + 99,
            "close": np.random.randn(100).cumsum() + 100,
            "volume": np.random.randint(1000, 10000, 100),
        }
    )
    sample_df["high"] = sample_df[["open", "close", "high"]].max(axis=1)
    sample_df["low"] = sample_df[["open", "close", "low"]].min(axis=1)

    X, _ = prepare_features(sample_df, config)
    return list(X.columns)
