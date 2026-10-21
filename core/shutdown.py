"""
Graceful shutdown handling for long-running processes.

Provides signal handlers for SIGTERM, SIGINT, and SIGBREAK (Windows)
that allow processes to clean up resources before exiting.
"""
import logging
import signal
import sys
import threading
import atexit
from typing import Callable, List, Optional

from core.logging_config import get_logger


logger = get_logger(__name__)

_shutdown_handlers: List[Callable] = []
_shutdown_event = threading.Event()
_is_shutting_down = False


def _logging_usable() -> bool:
    """Check whether console log output is still writable.

    At interpreter exit (atexit), stdout may already be closed (e.g. pytest
    closes capture streams before atexit runs). Emitting then triggers
    logging's own error tracebacks.
    """
    root = logging.getLogger()
    for handler in root.handlers:
        stream = getattr(handler, "stream", None)
        if stream is None:
            continue
        try:
            stream.write("")
            return True
        except (OSError, ValueError, TypeError):
            return False
    return True


def register_shutdown_handler(handler: Callable) -> None:
    """Register a function to be called on graceful shutdown.

    Handlers are called in reverse order of registration (LIFO).
    """
    _shutdown_handlers.append(handler)


def unregister_shutdown_handler(handler: Callable) -> bool:
    """Unregister a previously registered shutdown handler.

    Returns True if the handler was found and removed.
    """
    try:
        _shutdown_handlers.remove(handler)
        return True
    except ValueError:
        return False


def _run_shutdown_handlers() -> None:
    """Run all registered shutdown handlers."""
    global _is_shutting_down
    _is_shutting_down = True

    log_ok = _logging_usable()
    if log_ok:
        logger.info("Running shutdown handlers", extra={"handler_count": len(_shutdown_handlers)})

    for handler in reversed(_shutdown_handlers):
        try:
            handler()
        except Exception as e:
            if log_ok:
                logger.error("Shutdown handler failed", extra={"handler": str(handler), "error": str(e)}, exc_info=True)

    _shutdown_event.set()
    if log_ok:
        logger.info("All shutdown handlers completed")


def _signal_handler(signum: int, frame) -> None:
    """Handle shutdown signals."""
    signame = signal.Signals(signum).name
    logger.info(f"Received {signame}, initiating graceful shutdown", extra={"signal": signame})
    _run_shutdown_handlers()
    sys.exit(0)


def setup_signal_handlers() -> None:
    """Set up signal handlers for graceful shutdown."""
    # Register the atexit handler for normal exits
    atexit.register(_run_shutdown_handlers)

    # Handle common shutdown signals
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # Windows-specific signals
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _signal_handler)


def wait_for_shutdown(timeout: Optional[float] = None) -> bool:
    """Wait for the shutdown event to be set.

    Args:
        timeout: Optional timeout in seconds. None means wait indefinitely.

    Returns:
        True if shutdown event was set, False if timeout expired.
    """
    return _shutdown_event.wait(timeout)


def is_shutting_down() -> bool:
    """Check if shutdown has been initiated."""
    return _is_shutting_down


class GracefulShutdown:
    """Context manager for graceful shutdown handling.

    Usage:
        with GracefulShutdown() as shutdown:
            while not shutdown.is_shutting_down():
                do_work()
    """

    def __enter__(self) -> "GracefulShutdown":
        setup_signal_handlers()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if not _is_shutting_down:
            _run_shutdown_handlers()

    def is_shutting_down(self) -> bool:
        return is_shutting_down()

    def wait(self, timeout: Optional[float] = None) -> bool:
        return wait_for_shutdown(timeout)