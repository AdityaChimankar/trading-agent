"""
Circuit breaker pattern for external API calls.

Provides fault tolerance for Kite Connect and OpenRouter API calls
by failing fast when error rates exceed thresholds.
"""
import time
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, TypeVar, Optional

from core.logging_config import get_logger


logger = get_logger(__name__)

T = TypeVar("T")


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"      # Normal operation, requests pass through
    OPEN = "open"          # Failing fast, requests blocked
    HALF_OPEN = "half_open"  # Testing if service recovered


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker behavior."""
    failure_threshold: int = 5           # Number of failures before opening
    success_threshold: int = 2           # Successes needed to close from half-open
    timeout_seconds: float = 60.0        # Time before transitioning to half-open
    excluded_exceptions: tuple = ()      # Exception types that don't count as failures


@dataclass
class CircuitBreaker:
    """Circuit breaker for external service calls."""

    name: str
    config: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)

    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _failure_count: int = field(default=0, init=False)
    _success_count: int = field(default=0, init=False)
    _last_failure_time: float = field(default=0.0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    @property
    def state(self) -> CircuitState:
        """Get current state, checking for timeout transition."""
        with self._lock:
            if self._state == CircuitState.OPEN:
                if time.time() - self._last_failure_time >= self.config.timeout_seconds:
                    self._state = CircuitState.HALF_OPEN
                    self._success_count = 0
                    logger.info(f"Circuit breaker {self.name} transitioned to HALF_OPEN")
            return self._state

    def call(self, func: Callable[..., T], *args, **kwargs) -> T:
        """Execute function with circuit breaker protection.

        Args:
            func: Function to call
            *args: Positional arguments for func
            **kwargs: Keyword arguments for func

        Returns:
            Result of func

        Raises:
            CircuitOpenError: If circuit is open
            Original exception: If func raises an exception not in excluded_exceptions
        """
        state = self.state

        if state == CircuitState.OPEN:
            raise CircuitOpenError(f"Circuit breaker {self.name} is OPEN")

        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except self.config.excluded_exceptions:
            # These exceptions don't count as failures
            raise
        except Exception as e:
            self._on_failure()
            raise

    def _on_success(self) -> None:
        """Handle successful call."""
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.config.success_threshold:
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    logger.info(f"Circuit breaker {self.name} CLOSED after recovery")
            else:
                self._failure_count = 0

    def _on_failure(self) -> None:
        """Handle failed call."""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()

            if self._state == CircuitState.HALF_OPEN:
                # Any failure in half-open goes back to open
                self._state = CircuitState.OPEN
                logger.warning(f"Circuit breaker {self.name} re-OPENED after half-open failure")
            elif self._failure_count >= self.config.failure_threshold:
                self._state = CircuitState.OPEN
                logger.warning(
                    f"Circuit breaker {self.name} OPENED after {self._failure_count} failures",
                    extra={"failure_count": self._failure_count, "threshold": self.config.failure_threshold},
                )

    def reset(self) -> None:
        """Manually reset the circuit breaker to closed state."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._success_count = 0
            logger.info(f"Circuit breaker {self.name} manually reset to CLOSED")


class CircuitOpenError(Exception):
    """Raised when circuit breaker is open and calls are blocked."""
    pass


# Global circuit breakers for external services
_kite_circuit: Optional[CircuitBreaker] = None
_openrouter_circuit: Optional[CircuitBreaker] = None


def get_kite_circuit() -> CircuitBreaker:
    """Get the Kite Connect circuit breaker (singleton)."""
    global _kite_circuit
    if _kite_circuit is None:
        config = CircuitBreakerConfig(
            failure_threshold=5,
            success_threshold=2,
            timeout_seconds=60.0,
            excluded_exceptions=(KeyboardInterrupt, SystemExit),
        )
        _kite_circuit = CircuitBreaker("kite", config)
    return _kite_circuit


def get_openrouter_circuit() -> CircuitBreaker:
    """Get the OpenRouter API circuit breaker (singleton)."""
    global _openrouter_circuit
    if _openrouter_circuit is None:
        config = CircuitBreakerConfig(
            failure_threshold=3,
            success_threshold=2,
            timeout_seconds=120.0,
            excluded_exceptions=(KeyboardInterrupt, SystemExit),
        )
        _openrouter_circuit = CircuitBreaker("openrouter", config)
    return _openrouter_circuit


def call_with_kite_circuit(func: Callable[..., T], *args, **kwargs) -> T:
    """Execute a Kite API call with circuit breaker protection."""
    return get_kite_circuit().call(func, *args, **kwargs)


def call_with_openrouter_circuit(func: Callable[..., T], *args, **kwargs) -> T:
    """Execute an OpenRouter API call with circuit breaker protection."""
    return get_openrouter_circuit().call(func, *args, **kwargs)


def get_circuit_status() -> dict:
    """Get status of all circuit breakers for health checks."""
    return {
        "kite": {
            "state": get_kite_circuit().state.value,
            "failure_count": get_kite_circuit()._failure_count,
        },
        "openrouter": {
            "state": get_openrouter_circuit().state.value,
            "failure_count": get_openrouter_circuit()._failure_count,
        },
    }