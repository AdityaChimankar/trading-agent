"""Shared test fixtures and configuration."""
import pytest
import os
import sys
from pathlib import Path

# Ensure project root is in path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset singleton instances between tests."""
    # Reset config singleton
    import core.config as config_module
    config_module._config = None

    # Reset shutdown state
    import core.shutdown as shutdown_module
    shutdown_module._is_shutting_down = False
    shutdown_module._shutdown_event.clear()
    shutdown_module._shutdown_handlers.clear()

    yield

    # Cleanup after test
    config_module._config = None
    shutdown_module._is_shutting_down = False
    shutdown_module._shutdown_event.clear()
    shutdown_module._shutdown_handlers.clear()


@pytest.fixture
def sample_candles():
    """Create sample candle data for testing."""
    import pandas as pd
    import numpy as np
    from datetime import datetime, timedelta

    dates = pd.date_range(start="2024-01-01 09:15", periods=100, freq="5min")

    # Generate realistic OHLCV data
    close = 2500 + np.cumsum(np.random.randn(100) * 2)
    high = close + np.abs(np.random.randn(100) * 5)
    low = close - np.abs(np.random.randn(100) * 5)
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    volume = np.random.randint(1000, 10000, 100)

    df = pd.DataFrame({
        "timestamp": [d.strftime("%Y-%m-%dT%H:%M:00") for d in dates],
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    })

    return df