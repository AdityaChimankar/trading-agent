"""Pytest configuration and fixtures."""

import pytest
import asyncio
from datetime import datetime, timezone

import numpy as np
import pandas as pd


@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def sample_ohlcv_data():
    """Generate sample OHLCV data for testing."""
    np.random.seed(42)
    n = 500
    dates = pd.date_range("2024-01-01", periods=n, freq="5min", tz="UTC")

    close = np.cumsum(np.random.randn(n) * 0.5) + 1000
    high = close + np.abs(np.random.randn(n)) * 2
    low = close - np.abs(np.random.randn(n)) * 2
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    volume = np.random.randint(1000, 50000, n)

    df = pd.DataFrame({
        "timestamp": dates,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    })
    return df


@pytest.fixture
def sample_symbol_data():
    """Generate sample data for a single symbol."""
    np.random.seed(123)
    n = 2000
    dates = pd.date_range("2024-01-01", periods=n, freq="5min", tz="UTC")

    # Trending market
    trend = np.linspace(0, 50, n)
    noise = np.cumsum(np.random.randn(n) * 0.3)
    close = 1000 + trend + noise
    high = close + np.abs(np.random.randn(n)) * 1.5
    low = close - np.abs(np.random.randn(n)) * 1.5
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    volume = np.random.randint(5000, 100000, n)

    df = pd.DataFrame({
        "timestamp": dates,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    })
    df.attrs["symbol"] = "TESTSYM"
    return df


@pytest.fixture
def mock_candidate():
    """Create a mock candidate for testing."""
    from trading_agent.strategy.candidates import Candidate
    from datetime import datetime, timezone

    return Candidate(
        symbol="RELIANCE",
        timestamp=datetime.now(timezone.utc),
        interval="5minute",
        close=2500.0,
        rsi=35.0,
        adx=22.0,
        atr=45.0,
        pattern_bias=1,
        rule_action="BUY",
        rule_confidence=0.7,
        volume=50000,
    )

@pytest.fixture(autouse=True)
def _isolated_controls(tmp_path_factory, monkeypatch):
    """Tests run at any hour: lift the IST session window unless a test opts in,
    and keep the audit log / kill switch and the database out of the working tree.

    Every test gets a throwaway SQLite file. Without this, a test that starts the app
    without its own DB_URL opened the .env database: on 2026-10-26 a host test run
    took a Windows lock on the live stack's data/trading.db mid-session.
    """
    from trading_agent.core.config import get_settings
    from trading_agent.storage import db as db_module

    data = tmp_path_factory.mktemp("data")
    database = data / "test.db"
    monkeypatch.setenv("PAPER_ENFORCE_SESSION", "false")
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("DB_URL", f"sqlite:///{database.as_posix()}")
    monkeypatch.setattr(db_module, "DB_PATH", str(database))
    get_settings.cache_clear()
    db_module.reset_database()
    yield
    db_module.reset_database()
    get_settings.cache_clear()
