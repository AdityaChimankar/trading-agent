"""Unit tests for circuit breaker."""
import time

import pytest

from core.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
    CircuitOpenError,
    get_kite_circuit,
    get_openrouter_circuit,
    call_with_kite_circuit,
    call_with_openrouter_circuit,
    get_circuit_status,
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


def test_circuit_breaker_blocks_calls_when_open():
    """Test circuit breaker blocks calls when open."""
    cb = CircuitBreaker("test", CircuitBreakerConfig(failure_threshold=1))

    # Trigger open
    try:
        cb.call(lambda: (_ for _ in ()).throw(Exception("fail")))
    except Exception:
        pass

    # Next call should be blocked
    with pytest.raises(CircuitOpenError):
        cb.call(lambda: "success")


def test_circuit_breaker_half_open_after_timeout():
    """Test circuit breaker transitions to half-open after timeout."""
    cb = CircuitBreaker("test", CircuitBreakerConfig(failure_threshold=1, timeout_seconds=0.1))

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
        timeout_seconds=0.1,
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
        timeout_seconds=0.1,
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


def test_call_with_kite_circuit():
    """Test kite circuit helper."""
    result = call_with_kite_circuit(lambda x: x * 2, 5)
    assert result == 10


def test_call_with_openrouter_circuit():
    """Test openrouter circuit helper."""
    result = call_with_openrouter_circuit(lambda x: x + 1, 5)
    assert result == 6


def test_get_circuit_status():
    """Test circuit status helper."""
    status = get_circuit_status()
    assert "kite" in status
    assert "openrouter" in status
    assert status["kite"]["state"] in ("closed", "open", "half_open")
    assert status["openrouter"]["state"] in ("closed", "open", "half_open")