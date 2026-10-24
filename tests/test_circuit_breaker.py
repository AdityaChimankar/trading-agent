"""Unit tests for circuit breaker."""
import time

import pytest

from trading_agent.core.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
    CircuitBreakerError,
    get_circuit_breaker,
    get_all_circuit_breaker_stats,
)


def test_circuit_breaker_closed_by_default():
    """Test circuit breaker starts in closed state."""
    cb = CircuitBreaker("test", CircuitBreakerConfig(failure_threshold=3))
    assert cb.state == CircuitState.CLOSED


def test_circuit_breaker_opens_after_threshold():
    """Test circuit breaker opens after failure threshold."""
    cb = CircuitBreaker("test", CircuitBreakerConfig(failure_threshold=3))

    # Fail 3 times
    for _ in range(3):
        try:
            cb.call(lambda: (_ for _ in ()).throw(Exception("fail")))
        except Exception:
            pass

    assert cb.state == CircuitState.OPEN


def test_success_resets_consecutive_failure_count():
    cb = CircuitBreaker("consecutive", CircuitBreakerConfig(failure_threshold=2))

    with pytest.raises(RuntimeError):
        cb.call(lambda: (_ for _ in ()).throw(RuntimeError("first")))

    assert cb.call(lambda: "recovered") == "recovered"

    with pytest.raises(RuntimeError):
        cb.call(lambda: (_ for _ in ()).throw(RuntimeError("second")))

    assert cb.state == CircuitState.CLOSED


def test_circuit_breaker_blocks_calls_when_open():
    """Test circuit breaker blocks calls when open."""
    cb = CircuitBreaker("test", CircuitBreakerConfig(failure_threshold=1))

    # Trigger open
    try:
        cb.call(lambda: (_ for _ in ()).throw(Exception("fail")))
    except Exception:
        pass

    # Next call should be blocked
    with pytest.raises(CircuitBreakerError):
        cb.call(lambda: "success")


def test_circuit_breaker_half_open_after_timeout():
    """Test circuit breaker transitions to half-open after timeout."""
    cb = CircuitBreaker("test", CircuitBreakerConfig(failure_threshold=1, timeout=0.1))

    # Trigger open
    try:
        cb.call(lambda: (_ for _ in ()).throw(Exception("fail")))
    except Exception:
        pass

    assert cb.state == CircuitState.OPEN

    # Wait for timeout
    time.sleep(0.2)

    # Should transition to half-open
    assert cb.state == CircuitState.HALF_OPEN


def test_circuit_breaker_closes_after_successes():
    """Test circuit breaker closes after success threshold in half-open."""
    cb = CircuitBreaker("test", CircuitBreakerConfig(
        failure_threshold=1,
        success_threshold=2,
        timeout=0.1,
    ))

    # Trigger open
    try:
        cb.call(lambda: (_ for _ in ()).throw(Exception("fail")))
    except Exception:
        pass

    # Wait for timeout
    time.sleep(0.2)
    assert cb.state == CircuitState.HALF_OPEN

    # Two successes should close
    cb.call(lambda: "success")
    cb.call(lambda: "success")

    assert cb.state == CircuitState.CLOSED


def test_circuit_breaker_reopens_on_half_open_failure():
    """Test circuit breaker reopens on failure in half-open state."""
    cb = CircuitBreaker("test", CircuitBreakerConfig(
        failure_threshold=1,
        success_threshold=2,
        timeout=0.1,
    ))

    # Trigger open
    try:
        cb.call(lambda: (_ for _ in ()).throw(Exception("fail")))
    except Exception:
        pass

    # Wait for timeout
    time.sleep(0.2)
    assert cb.state == CircuitState.HALF_OPEN

    # Failure should reopen
    try:
        cb.call(lambda: (_ for _ in ()).throw(Exception("fail")))
    except Exception:
        pass

    assert cb.state == CircuitState.OPEN


def test_circuit_breaker_excluded_exceptions():
    """Test excluded exceptions don't count as failures."""
    cb = CircuitBreaker("test", CircuitBreakerConfig(
        failure_threshold=1,
        excluded_exceptions=(ValueError,),
    ))

    # ValueError should not count as failure
    try:
        cb.call(lambda: (_ for _ in ()).throw(ValueError("excluded")))
    except ValueError:
        pass

    assert cb.state == CircuitState.CLOSED
    assert cb._failure_count == 0


def test_circuit_breaker_reset():
    """Test manual reset."""
    cb = CircuitBreaker("test", CircuitBreakerConfig(failure_threshold=1))

    # Trigger open
    try:
        cb.call(lambda: (_ for _ in ()).throw(Exception("fail")))
    except Exception:
        pass

    assert cb.state == CircuitState.OPEN

    cb.reset()
    assert cb.state == CircuitState.CLOSED
    assert cb._failure_count == 0


def test_get_circuit_breaker():
    """Test get_circuit_breaker helper."""
    cb = get_circuit_breaker("test_kite", CircuitBreakerConfig(failure_threshold=2))
    assert isinstance(cb, CircuitBreaker)
    assert cb.name == "test_kite"


def test_get_all_circuit_breaker_stats():
    """Test circuit status helper."""
    get_circuit_breaker("test_stats_1")
    get_circuit_breaker("test_stats_2")
    stats = get_all_circuit_breaker_stats()
    assert "test_stats_1" in stats
    assert "test_stats_2" in stats
    # CircuitBreakerStats has total_calls, successful_calls, failed_calls, etc.
    assert stats["test_stats_1"].total_calls >= 0
