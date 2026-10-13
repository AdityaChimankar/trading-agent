"""Custom exceptions for the trading agent."""

from typing import Any


class TradingAgentError(Exception):
    """Base exception for trading agent errors."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        if self.details:
            return f"{self.message} | Details: {self.details}"
        return self.message


class ConfigurationError(TradingAgentError):
    """Configuration-related errors."""

    pass


class DataError(TradingAgentError):
    """Data-related errors (missing, corrupt, invalid)."""

    pass


class IngestionError(TradingAgentError):
    """Data ingestion errors."""

    pass


class KiteAPIError(TradingAgentError):
    """Kite Connect API errors."""

    def __init__(self, message: str, status_code: int | None = None, **details):
        super().__init__(message, details)
        self.status_code = status_code


class AuthenticationError(KiteAPIError):
    """Authentication/authorization errors."""

    pass


class RateLimitError(KiteAPIError):
    """Rate limit exceeded errors."""

    def __init__(self, message: str, retry_after: int | None = None, **details):
        super().__init__(message, **details)
        self.retry_after = retry_after


class StorageError(TradingAgentError):
    """Storage/backend errors."""

    pass


class ModelError(TradingAgentError):
    """ML model errors."""

    pass


class StrategyError(TradingAgentError):
    """Strategy execution errors."""

    pass


class RiskError(TradingAgentError):
    """Risk management errors."""

    pass


class ValidationError(TradingAgentError):
    """Input validation errors."""

    pass


class NotFoundError(TradingAgentError):
    """Resource not found errors."""

    pass


class CircuitBreakerError(TradingAgentError):
    """Circuit breaker open errors."""

    pass
