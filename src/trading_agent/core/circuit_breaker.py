"""Circuit breaker pattern for external service resilience."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from threading import Lock
from typing import Generic, TypeVar

from trading_agent.core.exceptions import CircuitBreakerError
from trading_agent.core.logging_config import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreakerConfig:
    """Circuit breaker configuration."""

    failure_threshold: int = 5
    success_threshold: int = 2
    timeout: float = 30.0
    excluded_exceptions: tuple[type[Exception], ...] = (
        ValueError,
        TypeError,
        KeyError,
    )


@dataclass
class CircuitBreakerStats:
    """Circuit breaker statistics."""

    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    rejected_calls: int = 0
    last_failure_time: float | None = None
    last_success_time: float | None = None
    state_changes: int = 0


class CircuitBreaker(Generic[T]):
    """Thread-safe circuit breaker implementation."""

    def __init__(
        self,
        name: str,
        config: CircuitBreakerConfig | None = None,
        fallback: Callable[..., T] | None = None,
    ):
        self.name = name
        self.config = config or CircuitBreakerConfig()
        self.fallback = fallback
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: float | None = None
        self._lock = Lock()
        self._stats = CircuitBreakerStats()

    def _check_state_transition(self) -> None:
        """Check and perform state transitions. Must be called with lock held."""
        if self._state == CircuitState.OPEN:
            if (
                self._last_failure_time
                and time.time() - self._last_failure_time >= self.config.timeout
            ):
                self._state = CircuitState.HALF_OPEN
                self._success_count = 0
                self._stats.state_changes += 1
                logger.warning("circuit_breaker_half_open", name=self.name)

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._check_state_transition()
            return self._state

    def _transition_to_open(self) -> None:
        """Transition from CLOSED/HALF_OPEN to OPEN."""
        self._state = CircuitState.OPEN
        self._last_failure_time = time.time()
        self._stats.state_changes += 1
        logger.warning("circuit_breaker_opened", name=self.name, failures=self._failure_count)

    def _transition_to_closed(self) -> None:
        """Transition from HALF_OPEN to CLOSED."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._stats.state_changes += 1
        logger.info("circuit_breaker_closed", name=self.name)

    def call(self, func: Callable[..., T], *args, **kwargs) -> T:
        """Execute function with circuit breaker protection."""
        with self._lock:
            self._stats.total_calls += 1

            self._check_state_transition()

            if self._state == CircuitState.OPEN:
                self._stats.rejected_calls += 1
                if self.fallback:
                    logger.info("circuit_breaker_fallback", name=self.name)
                    return self.fallback(*args, **kwargs)
                raise CircuitBreakerError(
                    f"Circuit breaker '{self.name}' is OPEN",
                    details={"name": self.name, "state": self._state.value},
                )

        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except self.config.excluded_exceptions:
            raise
        except Exception:
            self._on_failure()
            raise

    def _on_success(self) -> None:
        with self._lock:
            self._stats.successful_calls += 1
            self._stats.last_success_time = time.time()

            if self._state == CircuitState.CLOSED:
                # The failure threshold represents consecutive failures. A
                # successful call proves the dependency recovered.
                self._failure_count = 0
            elif self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.config.success_threshold:
                    self._transition_to_closed()

    def _on_failure(self) -> None:
        with self._lock:
            self._stats.failed_calls += 1
            self._stats.last_failure_time = time.time()
            self._last_failure_time = time.time()

            if self._state == CircuitState.HALF_OPEN:
                self._transition_to_open()
            elif self._state == CircuitState.CLOSED:
                self._failure_count += 1
                if self._failure_count >= self.config.failure_threshold:
                    self._transition_to_open()

    def reset(self) -> None:
        """Manually reset the circuit breaker."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self._last_failure_time = None
            logger.info("circuit_breaker_reset", name=self.name)

    def get_stats(self) -> CircuitBreakerStats:
        """Get circuit breaker statistics."""
        with self._lock:
            return CircuitBreakerStats(
                total_calls=self._stats.total_calls,
                successful_calls=self._stats.successful_calls,
                failed_calls=self._stats.failed_calls,
                rejected_calls=self._stats.rejected_calls,
                last_failure_time=self._stats.last_failure_time,
                last_success_time=self._stats.last_success_time,
                state_changes=self._stats.state_changes,
            )

    def __repr__(self) -> str:
        return f"CircuitBreaker(name={self.name}, state={self.state.value})"


class CircuitBreakerRegistry:
    """Registry for managing multiple circuit breakers."""

    def __init__(self):
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = Lock()

    def get_or_create(
        self,
        name: str,
        config: CircuitBreakerConfig | None = None,
        fallback: Callable | None = None,
    ) -> CircuitBreaker:
        """Get existing or create new circuit breaker."""
        with self._lock:
            if name not in self._breakers:
                self._breakers[name] = CircuitBreaker(name, config, fallback)
            return self._breakers[name]

    def get(self, name: str) -> CircuitBreaker | None:
        """Get circuit breaker by name."""
        with self._lock:
            return self._breakers.get(name)

    def remove(self, name: str) -> bool:
        """Remove circuit breaker."""
        with self._lock:
            if name in self._breakers:
                del self._breakers[name]
                return True
            return False

    def get_all_stats(self) -> dict[str, CircuitBreakerStats]:
        """Get stats for all circuit breakers."""
        with self._lock:
            return {name: cb.get_stats() for name, cb in self._breakers.items()}

    def reset_all(self) -> None:
        """Reset all circuit breakers."""
        with self._lock:
            for cb in self._breakers.values():
                cb.reset()


_registry = CircuitBreakerRegistry()


def get_circuit_breaker(
    name: str,
    config: CircuitBreakerConfig | None = None,
    fallback: Callable | None = None,
) -> CircuitBreaker:
    """Get or create a circuit breaker from the global registry."""
    return _registry.get_or_create(name, config, fallback)


def get_all_circuit_breaker_stats() -> dict[str, CircuitBreakerStats]:
    """Get stats for all registered circuit breakers."""
    return _registry.get_all_stats()
