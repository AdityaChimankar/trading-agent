"""Unit tests for core logging configuration."""
import logging
import json
from io import StringIO

from core.logging_config import (
    setup_logging,
    get_logger,
    JSONFormatter,
    log_decision_cycle,
    log_error_with_context,
    log_timed_operation,
)


def test_setup_logging_console(capfd):
    """Test logging setup with console output."""
    setup_logging(level=logging.DEBUG, json_format=False)
    logger = get_logger("test")
    logger.info("Test message")

    out, _ = capfd.readouterr()
    assert "Test message" in out
    assert "test" in out


def test_setup_logging_json(capfd):
    """Test logging setup with JSON format."""
    setup_logging(level=logging.INFO, json_format=True)
    logger = get_logger("test_json")
    logger.info("JSON test", extra={"symbol": "RELIANCE", "action": "BUY"})

    out, _ = capfd.readouterr()
    log_entry = json.loads(out.strip())
    assert log_entry["message"] == "JSON test"
    assert log_entry["symbol"] == "RELIANCE"
    assert log_entry["action"] == "BUY"
    assert log_entry["level"] == "INFO"


def test_json_formatter():
    """Test JSONFormatter directly."""
    formatter = JSONFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="Test message",
        args=(),
        exc_info=None,
    )
    record.symbol = "TCS"
    record.action = "SELL"

    formatted = formatter.format(record)
    data = json.loads(formatted)

    assert data["message"] == "Test message"
    assert data["symbol"] == "TCS"
    assert data["action"] == "SELL"
    assert data["level"] == "INFO"


def test_get_logger():
    """Test get_logger returns a logger instance."""
    logger = get_logger("test_module")
    assert isinstance(logger, logging.Logger)
    assert logger.name == "test_module"


def test_log_decision_cycle(capfd):
    """Test log_decision_cycle helper."""
    setup_logging(level=logging.INFO, json_format=False)
    logger = get_logger("test_decision")

    log_decision_cycle(logger, "test_cycle", "RELIANCE", "BUY", rsi=30, adx=25)

    out, _ = capfd.readouterr()
    assert "RELIANCE" in out
    assert "BUY" in out


def test_log_error_with_context(capfd):
    """Test log_error_with_context helper."""
    setup_logging(level=logging.ERROR, json_format=True)
    logger = get_logger("test_error")

    try:
        raise ValueError("Test error")
    except ValueError as e:
        log_error_with_context(logger, "Something went wrong", e, symbol="INFY")

    out, _ = capfd.readouterr()
    log_entry = json.loads(out.strip())
    assert log_entry["message"] == "Something went wrong"
    assert log_entry["symbol"] == "INFY"
    assert "Test error" in log_entry.get("exception", "")


def test_log_timed_operation(capfd):
    """Test log_timed_operation helper."""
    setup_logging(level=logging.INFO, json_format=False)
    logger = get_logger("test_timed")

    log_timed_operation(logger, "test_operation", 150.5, symbol="HDFC")

    out, _ = capfd.readouterr()
    assert "test_operation completed in 150.5ms" in out