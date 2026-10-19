"""Regression tests for historical candidate and exit contracts."""

from datetime import UTC, datetime

import pandas as pd

from trading_agent.research.backtest import BacktestEngine
from trading_agent.strategy.candidates import Candidate


class _Parquet:
    def __init__(self, candles: pd.DataFrame):
        self.candles = candles

    def read_candles(self, symbol, interval, start, end):
        return self.candles.copy()


class _Generator:
    def __init__(self):
        self.frames: list[pd.DataFrame] = []

    def generate_from_data(self, frame, symbol, interval):
        self.frames.append(frame.copy())
        if len(self.frames) > 1:
            return None
        latest = frame.iloc[-1]
        return Candidate(
            symbol=symbol,
            timestamp=latest["timestamp"],
            interval=interval,
            close=float(latest["close"]),
            rsi=30.0,
            adx=25.0,
            atr=2.0,
            pattern_bias=1,
            rule_action="BUY",
            rule_confidence=0.8,
            volume=100_000,
        )


def test_backtest_uses_historical_slice_and_retains_exit_levels():
    timestamps = pd.date_range(datetime(2026, 1, 1, tzinfo=UTC), periods=55, freq="5min")
    candles = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 100_000,
        }
    )
    candles.loc[50, "low"] = 90.0

    generator = _Generator()
    engine = BacktestEngine(candidate_generator=generator)
    engine.parquet = _Parquet(candles)
    result = engine.run(["TEST"], timestamps[0], timestamps[-1])

    assert generator.frames
    assert generator.frames[0]["timestamp"].max() == timestamps[49]
    assert generator.frames[0]["timestamp"].max() < candles["timestamp"].max()
    assert len(result.trades) == 1
    assert result.trades[0].stop_loss == 97.0
    assert result.trades[0].take_profit == 104.0
    assert result.trades[0].exit_reason == "stop_loss"
