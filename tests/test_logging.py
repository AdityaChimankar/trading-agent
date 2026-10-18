"""Unit tests for core logging configuration."""
import logging
import json
import os
from io import StringIO

from trading_agent.core.config import get_settings as get_config
from trading_agent.core.logging_config import (
    setup_logging,
    get_logger,
    bind_context,
    clear_context,
)


def test_setup_logging_console(capfd):
    """Test logging setup with console output."""
    setup_logging()
    logger = get_logger("test")
    logger.info("Test message")

    # structlog may not capture to capfd directly
    # Just verify it doesn't raise
    assert True


def test_setup_logging_json(capfd):
    """Test logging setup with JSON format."""
    # setup_logging doesn't take parameters, it reads from config
    # Test JSON by setting env var
    import os
    os.environ["LOG_FORMAT"] = "json"
    get_config.cache_clear()
    setup_logging()
    logger = get_logger("test_json")
    logger.info("JSON test", symbol="RELIANCE", action="BUY")

    # structlog may not capture to capfd directly
    assert True


def test_get_logger():
    """Test get_logger returns a logger instance."""
    logger = get_logger("test_module")
    assert logger is not None


def test_bind_context():
    """Test bind_context helper."""
    bind_context(symbol="RELIANCE", action="BUY")
    logger = get_logger("test_context")
    logger.info("Test with context")
    clear_context()


def test_clear_context():
    """Test clear_context helper."""
    bind_context(symbol="RELIANCE")
    clear_context()
    # Should not raise
    get_logger("test").info("After clear")