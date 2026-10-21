"""Unit tests for core configuration."""
import os
from pathlib import Path

import pytest

from trading_agent.core.config import (
    Settings as Config,
    KiteSettings as KiteConfig,
    DatabaseSettings as DatabaseConfig,
    APISettings as APIConfig,
    MLSettings,
    PaperRiskSettings,
    LoggingSettings as LoggingConfig,
    StorageSettings,
    get_settings as get_config,
)


@pytest.fixture(autouse=True)
def isolated_configuration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    prefixes = ("DB_", "KITE_", "STORAGE_", "API_", "PAPER_", "ML_", "LOG_", "LLM_")
    for key in tuple(os.environ):
        if key.startswith(prefixes) or key in {"DATA_DIR", "CACHE_DIR", "DEBUG", "ENVIRONMENT"}:
            monkeypatch.delenv(key)
    get_config.cache_clear()
    yield
    get_config.cache_clear()


def test_config_singleton():
    """Test that get_config returns a singleton."""
    config1 = get_config()
    config2 = get_config()
    assert config1 is config2


def test_reload_config():
    """Test that creating new Settings creates a new instance."""
    # Settings uses lru_cache, so we can't easily test reload
    # Just verify we can create a new instance
    config1 = get_config()
    config2 = Config()
    assert config1 is not config2


def test_kite_config_defaults(monkeypatch):
    """Test KiteSettings with environment variables."""
    monkeypatch.setenv("KITE_API_KEY", "test_key")
    monkeypatch.setenv("KITE_API_SECRET", "test_secret")

    # Need to clear cache to pick up new env vars
    get_config.cache_clear()
    config = get_config()
    assert config.kite.api_key == "test_key"
    assert config.kite.api_secret == "test_secret"


def test_ml_config(monkeypatch):
    """Test MLSettings."""
    monkeypatch.setenv("ML_FEATURE_VERSION", "v2")

    get_config.cache_clear()
    config = get_config()
    assert config.ml.feature_version == "v2"


def test_database_config_defaults():
    """Test DatabaseSettings default values."""
    get_config.cache_clear()
    config = get_config()
    assert "sqlite:///data/trading.db" in config.database.url
    assert config.database.echo is False
    assert config.database.pool_size == 5
    assert config.database.max_overflow == 10


def test_storage_config_defaults():
    """Test StorageSettings default values."""
    get_config.cache_clear()
    config = get_config()
    assert config.storage.parquet_root == Path("data/parquet")
    assert config.storage.candle_interval == "5minute"
    assert config.storage.compression == "snappy"


def test_api_config_defaults():
    """Test APISettings default values."""
    get_config.cache_clear()
    config = get_config()
    assert config.api.host == "127.0.0.1"
    assert config.api.port == 8000
    assert config.api.reload is False
    assert config.api.workers == 1


def test_logging_config(monkeypatch):
    """Test LoggingSettings."""
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("LOG_FORMAT", "json")

    get_config.cache_clear()
    config = get_config()
    assert config.logging.level == "DEBUG"
    assert config.logging.format == "json"


def test_api_config_override(monkeypatch):
    """Test that environment variables override defaults."""
    monkeypatch.setenv("API_HOST", "127.0.0.1")
    monkeypatch.setenv("API_PORT", "9000")

    get_config.cache_clear()
    config = get_config()
    assert config.api.host == "127.0.0.1"
    assert config.api.port == 9000


def test_env_override(monkeypatch):
    """Test that environment variables override defaults for nested settings."""
    monkeypatch.setenv("KITE_API_KEY", "env_key")
    monkeypatch.setenv("KITE_API_SECRET", "env_secret")
    monkeypatch.setenv("ML_CONFIDENCE_THRESHOLD", "0.6")

    get_config.cache_clear()
    config = get_config()
    assert config.kite.api_key == "env_key"
    assert config.kite.api_secret == "env_secret"
    assert config.ml.confidence_threshold == 0.6


@pytest.mark.parametrize("field", ["max_daily_loss_pct", "max_drawdown_pct", "max_single_position_pct"])
@pytest.mark.parametrize("value", [0, -1, 1.01, float("nan"), float("inf")])
def test_paper_limits_reject_invalid_fractions(field, value):
    with pytest.raises(ValueError):
        PaperRiskSettings(**{field: value})


def test_paper_limits_use_flat_environment_prefix(monkeypatch):
    monkeypatch.setenv("PAPER_MAX_DAILY_LOSS_PCT", "0.01")
    monkeypatch.setenv("PAPER_MAX_DRAWDOWN_PCT", "0.08")
    monkeypatch.setenv("PAPER_MAX_SINGLE_POSITION_PCT", "0.05")
    limits = get_config().paper
    assert (limits.max_daily_loss_pct, limits.max_drawdown_pct, limits.max_single_position_pct) == (.01, .08, .05)
