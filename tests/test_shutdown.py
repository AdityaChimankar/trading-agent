"""Unit tests for graceful shutdown."""
import asyncio
import threading
import time

from trading_agent.core.shutdown import (
    ShutdownManager,
    get_shutdown_manager,
)


def test_shutdown_manager_creation():
    """Test ShutdownManager creation."""
    manager = ShutdownManager()
    assert manager is not None
    assert not manager._shutdown_event.is_set()


def test_shutdown_manager_register_callback():
    """Test registering shutdown callbacks."""
    manager = ShutdownManager()
    callback_called = []

    def callback():
        callback_called.append(True)

    manager.register_callback(callback)
    assert len(manager._callbacks) == 1


def test_shutdown_manager_register_async_callback():
    """Test registering async shutdown callbacks."""
    manager = ShutdownManager()
    callback_called = []

    async def async_callback():
        callback_called.append(True)

    manager.register_async_callback(async_callback)
    assert len(manager._async_callbacks) == 1


def test_shutdown_manager_trigger():
    """Test triggering shutdown."""
    manager = ShutdownManager()
    assert not manager._shutdown_event.is_set()

    manager.shutdown()
    assert manager._shutdown_event.is_set()


def test_shutdown_manager_wait_for_shutdown():
    """Test waiting for shutdown."""
    manager = ShutdownManager()

    async def trigger_shutdown():
        await asyncio.sleep(0.1)
        manager.shutdown()

    async def test_wait():
        trigger_task = asyncio.create_task(trigger_shutdown())
        await manager.wait_for_shutdown()
        await trigger_task

    asyncio.run(test_wait())
    assert manager._shutdown_event.is_set()


def test_shutdown_manager_run_cleanup():
    """Test running cleanup callbacks."""
    manager = ShutdownManager()
    sync_called = []
    async_called = []

    def sync_callback():
        sync_called.append(True)

    async def async_callback():
        async_called.append(True)

    manager.register_callback(sync_callback)
    manager.register_async_callback(async_callback)
    manager.shutdown()

    asyncio.run(manager.run_cleanup())

    assert len(sync_called) == 1
    assert len(async_called) == 1


def test_shutdown_manager_lifespan():
    """Test ShutdownManager lifespan context manager."""
    async def test_lifespan():
        manager = ShutdownManager()
        async with manager.lifespan() as m:
            assert m is manager
            assert not m._shutdown_event.is_set()
            m.shutdown()
        # After exiting, cleanup should have run

    asyncio.run(test_lifespan())


def test_get_shutdown_manager_singleton():
    """Test get_shutdown_manager returns singleton."""
    manager1 = get_shutdown_manager()
    manager2 = get_shutdown_manager()
    assert manager1 is manager2


def test_shutdown_manager_signal_registration():
    """Test signal handler registration."""
    manager = ShutdownManager()
    # Should not raise
    manager.register_signals()
    assert manager._signals_registered is True