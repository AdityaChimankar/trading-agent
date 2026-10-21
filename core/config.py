"""
Centralized configuration management for the trading agent.

All configuration is loaded from environment variables (via .env file)
and validated at startup. This replaces scattered constants across modules.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from paths import PROJECT_ROOT

# Load .env file
load_dotenv(PROJECT_ROOT / ".env")


def _get_env(key: str, default: Optional[str] = None, required: bool = False) -> str:
    """Get environment variable with optional default and required flag."""
    value = os.getenv(key, default)
    if required and value is None:
        raise ValueError(f"Required environment variable {key} is not set")
    return value or ""


def _get_env_int(key: str, default: int) -> int:
    """Get environment variable as integer."""
    return int(os.getenv(key, str(default)))


def _get_env_float(key: str, default: float) -> float:
    """Get environment variable as float."""
    return float(os.getenv(key, str(default)))


def _get_env_bool(key: str, default: bool) -> bool:
    """Get environment variable as boolean."""
    val = os.getenv(key, str(default)).lower()
    return val in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class KiteConfig:
    """Kite Connect API configuration."""
    api_key: str = field(default_factory=lambda: _get_env("KITE_API_KEY", required=True))
    api_secret: str = field(default_factory=lambda: _get_env("KITE_API_SECRET", required=True))
    access_token_path: Path = field(default_factory=lambda: PROJECT_ROOT / ".access_token")


@dataclass(frozen=True)
class OpenRouterConfig:
    """OpenRouter API configuration."""
    api_key: str = field(default_factory=lambda: _get_env("OPENROUTER_API_KEY", required=True))


@dataclass(frozen=True)
class DatabaseConfig:
    """Database configuration."""
    path: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "trading_agent.db")
    wal_mode: bool = True
    busy_timeout_ms: int = 30000


@dataclass(frozen=True)
class RiskConfig:
    """Risk management configuration."""
    risk_per_trade_pct: float = field(default_factory=lambda: _get_env_float("RISK_PER_TRADE_PCT", 1.0))
    atr_stop_multiplier: float = field(default_factory=lambda: _get_env_float("ATR_STOP_MULTIPLIER", 1.5))
    reward_risk_ratio: float = field(default_factory=lambda: _get_env_float("REWARD_RISK_RATIO", 2.0))
    use_take_profit: bool = field(default_factory=lambda: _get_env_bool("USE_TAKE_PROFIT", False))
    use_position_sizing: bool = field(default_factory=lambda: _get_env_bool("USE_POSITION_SIZING", True))
    max_position_pct_of_capital: float = field(default_factory=lambda: _get_env_float("MAX_POSITION_PCT_OF_CAPITAL", 20.0))

    # Portfolio risk limits
    total_risk_budget_pct: float = field(default_factory=lambda: _get_env_float("TOTAL_RISK_BUDGET_PCT", 6.0))
    correlation_lookback: int = field(default_factory=lambda: _get_env_int("CORRELATION_LOOKBACK", 500))
    correlation_threshold: float = field(default_factory=lambda: _get_env_float("CORRELATION_THRESHOLD", 0.6))
    max_cluster_exposure_pct: float = field(default_factory=lambda: _get_env_float("MAX_CLUSTER_EXPOSURE_PCT", 20.0))
    # Total NOTIONAL bound, separate from the risk budget above. A book of five
    # 20%-of-capital positions risks little per trade and still commits the whole
    # book; this caps the sum of open position VALUE at 100% of capital by default.
    max_total_exposure_pct: float = field(default_factory=lambda: _get_env_float("MAX_TOTAL_EXPOSURE_PCT", 100.0))

    # Volatility-aware risk scaling
    volatility_risk_cutoff: float = field(default_factory=lambda: _get_env_float("VOLATILITY_RISK_CUTOFF", 0.80))
    volatility_risk_floor: float = field(default_factory=lambda: _get_env_float("VOLATILITY_RISK_FLOOR", 0.50))
    volatility_window_candles: int = field(default_factory=lambda: _get_env_int("VOLATILITY_WINDOW_CANDLES", 500))


@dataclass(frozen=True)
class SignalConfig:
    """Signal/decision configuration."""
    rsi_oversold: float = field(default_factory=lambda: _get_env_float("RSI_OVERSOLD", 30))
    rsi_overbought: float = field(default_factory=lambda: _get_env_float("RSI_OVERBOUGHT", 70))
    adx_threshold: float = field(default_factory=lambda: _get_env_float("ADX_THRESHOLD", 20))
    pattern_confirm_buy_rsi: float = field(default_factory=lambda: _get_env_float("PATTERN_CONFIRM_BUY_RSI", 45))
    pattern_confirm_sell_rsi: float = field(default_factory=lambda: _get_env_float("PATTERN_CONFIRM_SELL_RSI", 55))

    # Freshness
    max_candle_age_minutes: int = field(default_factory=lambda: _get_env_int("MAX_CANDLE_AGE_MINUTES", 10))
    heartbeat_max_age_sec: int = field(default_factory=lambda: _get_env_int("HEARTBEAT_MAX_AGE_SEC", 60))

    # Backtest/slippage
    slippage: float = field(default_factory=lambda: _get_env_float("SLIPPAGE", 0.0005))
    forward_window: int = field(default_factory=lambda: _get_env_int("FORWARD_WINDOW", 6))
    lookback_min: int = field(default_factory=lambda: _get_env_int("LOOKBACK_MIN", 20))
    swing_order: int = field(default_factory=lambda: _get_env_int("SWING_ORDER", 5))

    # Session/regime gates (the "when NOT to trade" layer from strategy/candidates.py)
    use_session_gates: bool = field(default_factory=lambda: _get_env_bool("USE_SESSION_GATES", False))
    session_gate_open_minute: int = field(default_factory=lambda: _get_env_int("SESSION_GATE_OPEN_MINUTE", 585))  # 09:45
    session_gate_close_minute: int = field(default_factory=lambda: _get_env_int("SESSION_GATE_CLOSE_MINUTE", 870))  # 14:30
    cost_cover_multiple: float = field(default_factory=lambda: _get_env_float("COST_COVER_MULTIPLE", 3.0))
    vol_regime_low_pctile: float = field(default_factory=lambda: _get_env_float("VOL_REGIME_LOW_PCTILE", 0.20))
    vol_regime_high_pctile: float = field(default_factory=lambda: _get_env_float("VOL_REGIME_HIGH_PCTILE", 0.80))
    gap_max_pct: float = field(default_factory=lambda: _get_env_float("GAP_MAX_PCT", 0.02))


@dataclass(frozen=True)
class IngestConfig:
    """Data ingestion configuration."""
    flush_interval_sec: int = field(default_factory=lambda: _get_env_int("FLUSH_INTERVAL_SEC", 5))
    stall_warn_sec: int = field(default_factory=lambda: _get_env_int("STALL_WARN_SEC", 90))
    force_reconnect_sec: int = field(default_factory=lambda: _get_env_int("FORCE_RECONNECT_SEC", 300))
    reconnect_max_tries: int = field(default_factory=lambda: _get_env_int("RECONNECT_MAX_TRIES", 300))
    reconnect_max_delay_sec: int = field(default_factory=lambda: _get_env_int("RECONNECT_MAX_DELAY_SEC", 30))
    large_watchlist_threshold: int = field(default_factory=lambda: _get_env_int("LARGE_WATCHLIST_THRESHOLD", 100))
    default_interval: str = field(default_factory=lambda: _get_env("DEFAULT_INTERVAL", "minute"))
    default_days_back: int = field(default_factory=lambda: _get_env_int("DEFAULT_DAYS_BACK", 60))


@dataclass(frozen=True)
class SchedulerConfig:
    """Scheduler configuration."""
    heartbeat_trust_sec: int = field(default_factory=lambda: _get_env_int("HEARTBEAT_TRUST_SEC", 90))
    feed_down_lookback_min: int = field(default_factory=lambda: _get_env_int("FEED_DOWN_LOOKBACK_MIN", 45))
    max_workers: int = field(default_factory=lambda: _get_env_int("MAX_WORKERS", 4))


@dataclass(frozen=True)
class LoggingConfig:
    """Logging configuration."""
    level: str = field(default_factory=lambda: _get_env("LOG_LEVEL", "INFO"))
    json_format: bool = field(default_factory=lambda: _get_env_bool("LOG_JSON", False))
    log_file: Optional[str] = field(default_factory=lambda: _get_env("LOG_FILE", None))


@dataclass(frozen=True)
class APIConfig:
    """API server configuration."""
    host: str = field(default_factory=lambda: _get_env("API_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _get_env_int("API_PORT", 8000))
    cors_origins: list = field(default_factory=lambda: _get_env("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(","))


@dataclass(frozen=True)
class Config:
    """Main configuration container."""
    kite: KiteConfig = field(default_factory=KiteConfig)
    openrouter: OpenRouterConfig = field(default_factory=OpenRouterConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    signal: SignalConfig = field(default_factory=SignalConfig)
    ingest: IngestConfig = field(default_factory=IngestConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    api: APIConfig = field(default_factory=APIConfig)


# Global config instance
_config: Optional[Config] = None


def get_config() -> Config:
    """Get the global configuration instance (singleton)."""
    global _config
    if _config is None:
        _config = Config()
    return _config


def reload_config() -> Config:
    """Reload configuration from environment variables."""
    global _config
    _config = Config()
    return _config