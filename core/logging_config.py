"""
Centralized logging configuration for the trading agent.

Provides structured JSON logging for production and human-readable
console output for development. All modules should use `get_logger(__name__)`
instead of print statements.
"""
import logging
import logging.handlers
import sys
import os
from pathlib import Path
from typing import Optional

from paths import PROJECT_ROOT


class JSONFormatter(logging.Formatter):
    """JSON formatter for structured logging."""

    def format(self, record: logging.LogRecord) -> str:
        import json
        import time

        log_data = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add extra fields if present
        if hasattr(record, "symbol"):
            log_data["symbol"] = record.symbol
        if hasattr(record, "action"):
            log_data["action"] = record.action
        if hasattr(record, "duration_ms"):
            log_data["duration_ms"] = record.duration_ms
        if hasattr(record, "error"):
            log_data["error"] = str(record.error)

        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_data)


def setup_logging(
    level: int = logging.INFO,
    json_format: bool = False,
    log_file: Optional[str] = None,
) -> None:
    """Configure root logger with console and optional file output.

    Args:
        level: Logging level (default INFO)
        json_format: If True, use JSON formatting for structured logs
        log_file: Optional path to log file (relative to project root)
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Clear existing handlers
    root_logger.handlers.clear()

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    if json_format:
        console_handler.setFormatter(JSONFormatter())
    else:
        console_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                datefmt="%H:%M:%S",
            )
        )
    root_logger.addHandler(console_handler)

    # File handler (optional)
    if log_file:
        log_path = PROJECT_ROOT / log_file
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=10_000_000, backupCount=5
        )
        file_handler.setFormatter(JSONFormatter())
        root_logger.addHandler(file_handler)

    # Suppress noisy third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance for the given module name.

    Usage:
        logger = get_logger(__name__)
        logger.info("Starting cycle", extra={"symbol": "RELIANCE"})
    """
    return logging.getLogger(name)


# Convenience functions for common patterns
def log_decision_cycle(logger: logging.Logger, cycle_name: str, symbol: str, action: str, **kwargs):
    """Log a decision cycle result."""
    extra = {"symbol": symbol, "action": action, "cycle": cycle_name, **kwargs}
    logger.info(f"{cycle_name} decision: {symbol} -> {action}", extra=extra)


def log_error_with_context(logger: logging.Logger, message: str, error: Exception, **kwargs):
    """Log an error with structured context."""
    extra = {"error": str(error), **kwargs}
    logger.error(message, extra=extra, exc_info=True)


def log_timed_operation(logger: logging.Logger, operation: str, duration_ms: float, **kwargs):
    """Log a timed operation."""
    extra = {"duration_ms": duration_ms, **kwargs}
    logger.info(f"{operation} completed in {duration_ms:.1f}ms", extra=extra)