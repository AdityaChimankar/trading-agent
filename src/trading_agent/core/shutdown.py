"""Graceful shutdown handling."""

from __future__ import annotations

import asyncio
import signal
import sys
from collections.abc import Callable
from contextlib import asynccontextmanager

from trading_agent.core.logging_config import get_logger

logger = get_logger(__name__)


class ShutdownManager:
    """Manage graceful shutdown of async applications."""

    def __init__(self):
        self._shutdown_event = asyncio.Event()
        self._callbacks: list[Callable[[], None]] = []
        self._async_callbacks: list[Callable[[], asyncio.coroutine]] = []
        self._signals_registered = False

    def register_callback(self, callback: Callable[[], None]) -> None:
        """Register a synchronous cleanup callback."""
        self._callbacks.append(callback)

    def register_async_callback(self, callback: Callable[[], asyncio.coroutine]) -> None:
        """Register an asynchronous cleanup callback."""
        self._async_callbacks.append(callback)

    def register_signals(self) -> None:
        """Register signal handlers for graceful shutdown."""
        if self._signals_registered:
            return

        if sys.platform == "win32":
            # Windows doesn't support add_signal_handler
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, self._signal_handler)
        else:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self._signal_handler)

        self._signals_registered = True

    def _signal_handler(self, signum: int, frame) -> None:
        """Handle shutdown signals."""
        logger.info("shutdown_signal_received", signal=signum)
        self.shutdown()

    def shutdown(self) -> None:
        """Trigger shutdown."""
        if not self._shutdown_event.is_set():
            logger.info("shutdown_initiated")
            self._shutdown_event.set()

    async def wait_for_shutdown(self) -> None:
        """Wait for shutdown signal."""
        await self._shutdown_event.wait()

    async def run_cleanup(self) -> None:
        """Run all registered cleanup callbacks."""
        logger.info(
            "running_cleanup",
            sync_callbacks=len(self._callbacks),
            async_callbacks=len(self._async_callbacks),
        )

        # Run sync callbacks
        for callback in self._callbacks:
            try:
                callback()
            except Exception as e:
                logger.error("cleanup_callback_failed", error=str(e))

        # Run async callbacks
        for callback in self._async_callbacks:
            try:
                await callback()
            except Exception as e:
                logger.error("async_cleanup_callback_failed", error=str(e))

        logger.info("cleanup_complete")

    @asynccontextmanager
    async def lifespan(self):
        """Async context manager for application lifespan."""
        self.register_signals()
        try:
            yield self
        finally:
            await self.run_cleanup()


# Global shutdown manager
_shutdown_manager: ShutdownManager | None = None


def get_shutdown_manager() -> ShutdownManager:
    """Get or create the global shutdown manager."""
    global _shutdown_manager
    if _shutdown_manager is None:
        _shutdown_manager = ShutdownManager()
    return _shutdown_manager
