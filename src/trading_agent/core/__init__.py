"""Core module - Shared types, utilities, and base classes."""

from trading_agent.core.config import Settings, get_settings
from trading_agent.core.exceptions import (
    AuthenticationError,
    CircuitBreakerError,
    ConfigurationError,
    DataError,
    IngestionError,
    KiteAPIError,
    ModelError,
    NotFoundError,
    RateLimitError,
    RiskError,
    StorageError,
    StrategyError,
    TradingAgentError,
    ValidationError,
)
from trading_agent.core.logging_config import bind_context, clear_context, get_logger, setup_logging

__all__ = [
    # Config
    "Settings",
    "get_settings",
    # Exceptions
    "TradingAgentError",
    "ConfigurationError",
    "DataError",
    "IngestionError",
    "KiteAPIError",
    "AuthenticationError",
    "RateLimitError",
    "StorageError",
    "ModelError",
    "StrategyError",
    "RiskError",
    "ValidationError",
    "NotFoundError",
    "CircuitBreakerError",
    # Logging
    "setup_logging",
    "get_logger",
    "bind_context",
    "clear_context",
]
