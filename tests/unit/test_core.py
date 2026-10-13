"""Tests for core modules."""

import pytest
import numpy as np
import pandas as pd
from datetime import datetime, timezone

from trading_agent.core.quant_indicators import (
    sma, ema, rsi, atr, adx, bollinger_bands, macd, stochastic,
    prepare_indicators, swings, pattern_detection,
)
from trading_agent.core.pattern_detection import (
    detect_single_candle_patterns, find_swings, compute_pattern_bias,
)
from trading_agent.core.signals import evaluate_signals, SignalParams
from trading_agent.core.position_sizing import (
    calculate_position_size, PositionSizingConfig, calculate_kelly_fraction,
)
from trading_agent.core.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitState
from trading_agent.core.freshness import FreshnessMonitor, FreshnessConfig, FreshnessStatus


class TestQuantIndicators:
    """Test quantitative indicators."""

    def setup_method(self):
        np.random.seed(42)
        n = 100
        self.close = np.cumsum(np.random.randn(n)) + 100
        self.high = self.close + np.abs(np.random.randn(n)) * 0.5
        self.low = self.close - np.abs(np.random.randn(n)) * 0.5
        self.open = np.roll(self.close, 1)
        self.open[0] = self.close[0]
        self.volume = np.random.randint(1000, 10000, n)

    def test_sma(self):
        result = sma(self.close, 20)
        assert len(result) == len(self.close)
        assert np.isnan(result[:19]).all()
        assert not np.isnan(result[19:]).any()

    def test_ema(self):
        result = ema(self.close, 20)
        assert len(result) == len(self.close)
        assert not np.isnan(result).any()

    def test_rsi(self):
        result = rsi(self.close, 14)
        assert len(result) == len(self.close)
        assert np.isnan(result[0])
        assert (result[1:] >= 0).all()
        assert (result[1:] <= 100).all()

    def test_atr(self):
        result = atr(self.high, self.low, self.close, 14)
        assert len(result) == len(self.close)
        assert np.isnan(result[0])
        assert (result[1:] >= 0).all()

    def test_adx(self):
        result = adx(self.high, self.low, self.close, 14)
        assert len(result) == len(self.close)
        assert (result >= 0).all()
        assert (result <= 100).all()

    def test_bollinger_bands(self):
        upper, middle, lower = bollinger_bands(self.close, 20)
        assert len(upper) == len(self.close)
        # Skip NaN values in comparison (first period-1 elements are NaN)
        valid = ~np.isnan(upper) & ~np.isnan(middle) & ~np.isnan(lower)
        assert (upper[valid] >= middle[valid]).all()
        assert (middle[valid] >= lower[valid]).all()

    def test_macd(self):
        macd_line, signal, hist = macd(self.close)
        assert len(macd_line) == len(self.close)
        assert len(signal) == len(self.close)
        assert len(hist) == len(self.close)

    def test_stochastic(self):
        k, d = stochastic(self.high, self.low, self.close)
        assert len(k) == len(self.close)
        assert len(d) == len(self.close)

    def test_prepare_indicators(self):
        df = pd.DataFrame({
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        })
        result = prepare_indicators(df)
        assert "rsis" in result
        assert "adxs" in result
        assert "atrs" in result
        assert result["length"] == len(self.close)

    def test_swings(self):
        highs, lows = swings(self.high, self.low, order=5)
        assert isinstance(highs, list)
        assert isinstance(lows, list)

    def test_pattern_detection(self):
        hammers, bull_engulf, bear_engulf = pattern_detection(
            self.open, self.high, self.low, self.close
        )
        assert hammers.dtype == bool
        assert bull_engulf.dtype == bool
        assert bear_engulf.dtype == bool


class TestPatternDetection:
    """Test pattern detection."""

    def test_hammer_detection(self):
        open_ = np.array([100, 100, 100])
        high = np.array([100.05, 100.05, 101])  # Second candle has tiny upper shadow
        low = np.array([98, 95, 99])  # Second candle has long lower shadow
        close = np.array([100.5, 99.5, 100])

        hammers, bull_engulf, bear_engulf = detect_single_candle_patterns(open_, high, low, close)
        assert hammers[1] == True  # Hammer at index 1

    def test_bullish_engulfing(self):
        open_ = np.array([100, 99])
        high = np.array([101, 101])
        low = np.array([99, 98])
        close = np.array([99, 100.5])  # Second bullish engulfs first bearish

        hammers, bull_engulf, bear_engulf = detect_single_candle_patterns(open_, high, low, close)
        assert bull_engulf[1] == True

    def test_bearish_engulfing(self):
        open_ = np.array([100, 101])
        high = np.array([101, 102])
        low = np.array([99, 99])
        close = np.array([101, 99.5])  # Second bearish engulfs first bullish

        hammers, bull_engulf, bear_engulf = detect_single_candle_patterns(open_, high, low, close)
        assert bear_engulf[1] == True

    def test_compute_pattern_bias(self):
        n = 50
        open_ = np.full(n, 100.0)
        high = np.full(n, 101.0)
        low = np.full(n, 99.0)
        close = np.full(n, 100.0)

        # Add a hammer at index 10
        low[10] = 97.0
        high[10] = 100.05  # Tiny upper shadow for hammer
        close[10] = 100.5

        # Add bearish engulfing at index 20
        open_[19], close[19] = 100, 101
        open_[20], close[20] = 101, 99

        bias = compute_pattern_bias(open_, high, low, close)
        assert bias[10] == 1  # Bullish (hammer)
        assert bias[20] == 2  # Bearish (engulfing)


class TestSignals:
    """Test signal evaluation."""

    def setup_method(self):
        self.rsi = np.array([25, 35, 45, 55, 65, 75])
        self.adx = np.array([25, 25, 25, 25, 25, 25])
        self.pattern = np.array([1, 1, 0, 2, 2, 2])  # bullish, bullish, neutral, bearish, bearish, bearish

    def test_neutral_sentiment_buy(self):
        params = SignalParams(pattern_confirm_buy_rsi=45)
        actions = evaluate_signals(self.rsi, self.adx, self.pattern, params=params)
        assert actions[0] == 1  # RSI 25 < 45, bullish pattern
        assert actions[1] == 1  # RSI 35 < 45, bullish pattern
        assert actions[2] == 0  # Neutral pattern

    def test_neutral_sentiment_sell(self):
        params = SignalParams(pattern_confirm_sell_rsi=55)
        actions = evaluate_signals(self.rsi, self.adx, self.pattern, params=params)
        assert actions[3] == 0  # RSI 55 not > 55, no sell
        assert actions[4] == 2  # RSI 65 > 55, bearish pattern
        assert actions[5] == 2  # RSI 75 > 55, bearish pattern

    def test_adx_filter(self):
        adx_low = np.array([15, 15, 15, 15, 15, 15])
        params = SignalParams(adx_threshold=20, pattern_confirm_buy_rsi=45)
        actions = evaluate_signals(self.rsi, adx_low, self.pattern, params=params)
        assert (actions == 0).all()  # All filtered out by ADX

    def test_nan_handling(self):
        rsi_nan = np.array([25, np.nan, 45])
        adx_nan = np.array([25, 25, np.nan])
        pattern_nan = np.array([1, 1, 1])
        actions = evaluate_signals(rsi_nan, adx_nan, pattern_nan)
        assert actions[1] == 0  # NaN RSI -> HOLD
        assert actions[2] == 0  # NaN ADX -> HOLD


class TestPositionSizing:
    """Test position sizing."""

    def test_basic_sizing(self):
        result = calculate_position_size(
            entry_price=1000,
            atr_value=20,
            portfolio_value=1000000,
        )
        assert result.shares > 0
        assert result.stop_price == 970  # 1000 - 20*1.5
        assert result.risk_amount > 0

    def test_volatility_adjustment(self):
        config = PositionSizingConfig(reduce_size_high_vol=True, max_atr_percentile=0.75)
        result_high = calculate_position_size(
            entry_price=1000, atr_value=20, portfolio_value=1000000,
            config=config, atr_percentile=0.9,
        )
        result_normal = calculate_position_size(
            entry_price=1000, atr_value=20, portfolio_value=1000000,
            config=config, atr_percentile=0.5,
        )
        assert result_high.shares <= result_normal.shares

    def test_kelly_fraction(self):
        kelly = calculate_kelly_fraction(win_rate=0.55, avg_win=0.02, avg_loss=0.015)
        assert 0 < kelly < 1


class TestCircuitBreaker:
    """Test circuit breaker."""

    def test_closed_state(self):
        cb = CircuitBreaker("test", CircuitBreakerConfig(failure_threshold=3))
        assert cb.state == CircuitState.CLOSED

        # Successful calls
        result = cb.call(lambda: "success")
        assert result == "success"
        assert cb.state == CircuitState.CLOSED

    def test_opens_after_failures(self):
        cb = CircuitBreaker("test", CircuitBreakerConfig(failure_threshold=2))

        # First failure
        try:
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("fail")))
        except RuntimeError:
            pass
        assert cb.state == CircuitState.CLOSED
        assert cb._failure_count == 1

        # Second failure - should open
        try:
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("fail")))
        except RuntimeError:
            pass
        assert cb.state == CircuitState.OPEN

    def test_fallback(self):
        cb = CircuitBreaker("test", CircuitBreakerConfig(failure_threshold=1), fallback=lambda: "fallback")

        # First failure - should open and use fallback on next call
        try:
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("fail")))
        except RuntimeError:
            pass

        # Now circuit is open, should use fallback
        result = cb.call(lambda: "success")
        assert result == "fallback"


class TestFreshnessMonitor:
    """Test freshness monitor."""

    def test_fresh(self):
        monitor = FreshnessMonitor(FreshnessConfig(fresh_threshold_seconds=300))
        monitor.update("test_source")
        result = monitor.check("test_source")
        assert result.status == FreshnessStatus.FRESH

    def test_stale(self):
        monitor = FreshnessMonitor(FreshnessConfig(fresh_threshold_seconds=1, stale_threshold_seconds=10))
        monitor.update("test_source", datetime.now(timezone.utc) - pd.Timedelta(seconds=5))
        result = monitor.check("test_source")
        assert result.status == FreshnessStatus.STALE

    def test_critical(self):
        monitor = FreshnessMonitor(FreshnessConfig(fresh_threshold_seconds=5, stale_threshold_seconds=10, critical_threshold_seconds=10))
        monitor.update("test_source", datetime.now(timezone.utc) - pd.Timedelta(seconds=20))
        result = monitor.check("test_source")
        assert result.status == FreshnessStatus.CRITICAL

    def test_unknown(self):
        monitor = FreshnessMonitor()
        result = monitor.check("unknown_source")
        assert result.status == FreshnessStatus.UNKNOWN


if __name__ == "__main__":
    pytest.main([__file__, "-v"])