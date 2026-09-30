"""Unit tests for graceful shutdown."""
import threading
import time

from core.shutdown import (
    register_shutdown_handler,
    unregister_shutdown_handler,
    setup_signal_handlers,
    wait_for_shutdown,
    is_shutting_down,
    GracefulShutdown,
)


def test_register_unregister_handler():
    """Test registering and unregistering shutdown handlers."""
    handler_called = []

    def handler():
        handler_called.append(True)

    register_shutdown_handler(handler)
    unregister_shutdown_handler(handler)

    # Manually call handlers to verify
    from core.shutdown import _run_shutdown_handlers
    _run_shutdown_handlers()

    # Handler should not have been called since it was unregistered
    assert len(handler_called) == 0


def test_shutdown_handler_execution():
    """Test shutdown handlers are executed."""
    handler_called = []

    def handler():
        handler_called.append(True)

    register_shutdown_handler(handler)

    from core.shutdown import _run_shutdown_handlers
    _run_shutdown_handlers()

    assert len(handler_called) == 1


def test_is_shutting_down():
    """Test is_shutting_down flag."""
    assert is_shutting_down() is False

    from core.shutdown import _run_shutdown_handlers
    _run_shutdown_handlers()

    assert is_shutting_down() is True


def test_wait_for_shutdown():
    """Test wait_for_shutdown."""
    # Reset shutdown state for this test
    import core.shutdown as sd
    sd._is_shutting_down = False
    sd._shutdown_event.clear()

    # Start a thread to trigger shutdown
    def trigger_shutdown():
        time.sleep(0.1)
        from core.shutdown import _run_shutdown_handlers
        _run_shutdown_handlers()

    thread = threading.Thread(target=trigger_shutdown)
    thread.start()

    result = wait_for_shutdown(timeout=1.0)
    assert result is True
    thread.join()


def test_graceful_shutdown_context_manager():
    """Test GracefulShutdown context manager."""
    with GracefulShutdown() as shutdown:
        assert shutdown.is_shutting_down() is False
        # Simulate shutdown
        from core.shutdown import _run_shutdown_handlers
        _run_shutdown_handlers()
        assert shutdown.is_shutting_down() is True