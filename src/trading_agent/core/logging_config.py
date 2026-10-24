"""Structured logging configuration."""

import logging
import sys
from pathlib import Path
from typing import Any

import structlog
from structlog.types import EventDict, WrappedLogger

from trading_agent.core.config import get_settings


def add_service_name(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    """Add service name to all log entries."""
    event_dict["service"] = "trading-agent"
    return event_dict


def add_timestamp(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    """Add ISO timestamp to log entries."""
    import datetime

    event_dict["timestamp"] = datetime.datetime.now(datetime.UTC).isoformat()
    return event_dict


def drop_color_message_key(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """Remove color_message key added by structlog.dev.ConsoleRenderer."""
    event_dict.pop("color_message", None)
    return event_dict


def setup_logging() -> None:
    """Configure structured logging for the application."""
    settings = get_settings()

    # Standard library logging setup
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, settings.logging.level),
    )

    # Structlog processors
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        add_service_name,
        add_timestamp,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        drop_color_message_key,
    ]

    if settings.logging.format == "json":
        processors = shared_processors + [
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ]
    else:
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer(colors=True),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Configure file logging if specified
    if settings.logging.file:
        log_path = Path(settings.logging.file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        file_handler = logging.FileHandler(log_path)
        file_handler.setLevel(getattr(logging, settings.logging.level))
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )
        logging.getLogger().addHandler(file_handler)

    # Reduce noise from third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("playwright").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a structured logger instance."""
    return structlog.get_logger(name)


# Context variables for request/session tracking
def bind_context(**kwargs: Any) -> None:
    """Bind context variables to current logging context."""
    structlog.contextvars.bind_contextvars(**kwargs)


def clear_context() -> None:
    """Clear all context variables."""
    structlog.contextvars.clear_contextvars()
