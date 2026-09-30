"""Unit tests for core configuration."""
import os
from pathlib import Path

import pytest

from core.config import (
    Config,
    KiteConfig,
    OpenRouterConfig,
    DatabaseConfig,
    RiskConfig,
    SignalConfig,
    IngestConfig,
    SchedulerConfig,
    LoggingConfig,
    APIConfig,
    get_config,
    reload_config,
)


def test_config_singleton():
    """Test that get_config returns a singleton."""
    config1 = get_config()
    config2 = get_config()
    assert config1 is config2


def test_reload_config():
    """Test that reload_config creates a new instance."""
    config1 = get_config()
    config2 = reload_config()
    assert config1 is not config2


def test_kite_config_defaults():
    """Test KiteConfig with environment variables."""
    os.environ["KITE_API_KEY"] = "test_key"
    os.environ["KITE_API_SECRET"] = "test_secret"

    config = reload_config()
    assert config.kite.api_key == "test_key"
    assert config.kite.api_secret == "test_secret"


def test_openrouter_config():
    """Test OpenRouterConfig."""
    os.environ["OPENROUTER_API_KEY"] = "test_openrouter_key"

    config = reload_config()
    assert config.openrouter.api_key == "test_openrouter_key"


def test_risk_config_defaults():
    """Test RiskConfig default values."""
    config = reload_config()
    assert config.risk.risk_per_trade_pct == 1.0
    assert config.risk.atr_stop_multiplier == 1.5
    assert config.risk.reward_risk_ratio == 2.0
    assert config.risk.use_take_profit is False


def test_signal_config_defaults():
    """Test SignalConfig default values."""
    config = reload_config()
    assert config.signal.rsi_oversold == 30
    assert config.signal.rsi_overbought == 70
    assert config.signal.adx_threshold == 20


def test_ingest_config_defaults():
    """Test IngestConfig default values."""
    config = reload_config()
    assert config.ingest.flush_interval_sec == 5
    assert config.ingest.stall_warn_sec == 90
    assert config.ingest.force_reconnect_sec == 300


def test_scheduler_config_defaults():
    """Test SchedulerConfig default values."""
    config = reload_config()
    assert config.scheduler.heartbeat_trust_sec == 90
    assert config.scheduler.feed_down_lookback_min == 45


def test_logging_config():
    """Test LoggingConfig."""
    os.environ["LOG_LEVEL"] = "DEBUG"
    os.environ["LOG_JSON"] = "true"

    config = reload_config()
    assert config.logging.level == "DEBUG"
    assert config.logging.json_format is True


def test_api_config():
    """Test APIConfig."""
    os.environ["API_HOST"] = "127.0.0.1"
    os.environ["API_PORT"] = "9000"

    config = reload_config()
    assert config.api.host == "127.0.0.1"
    assert config.api.port == 9000


def test_env_override():
    """Test that environment variables override defaults."""
    os.environ["RISK_PER_TRADE_PCT"] = "2.5"
    os.environ["ATR_STOP_MULTIPLIER"] = "2.0"

    config = reload_config()
    assert config.risk.risk_per_trade_pct == 2.5
    assert config.risk.atr_stop_multiplier == 2.0