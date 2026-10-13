"""Unit tests for core signals module (EMA pullback strategy)."""
import numpy as np
import pandas as pd

from core.signals import (
    LOOKBACK_MIN,
    FORWARD_WINDOW,
    SWING_ORDER,
    NEUTRAL_SENTIMENT,
    SLIPPAGE,
    DEFAULT_PARAMS,
    PATTERN_COLS_BULLISH,
    PATTERN_COLS_BEARISH,
    prepare_symbol_series,
    precompute_pattern_bias_codes,
    vectorized_evaluate,
    simulate_trades,
    verify_fast_path_matches_reference,
    MAX_WORKERS,
)


def test_constants():
    """Test that constants have expected values."""
    assert LOOKBACK_MIN == 30
    assert FORWARD_WINDOW == 6
    assert SWING_ORDER == 5
    assert NEUTRAL_SENTIMENT == 0.0
    assert SLIPPAGE == 0.0005
    assert MAX_WORKERS == 4


def test_default_params():
    """Test DEFAULT_PARAMS structure for EMA pullback strategy."""
    assert "htf_ema_fast" in DEFAULT_PARAMS
    assert "htf_ema_slow" in DEFAULT_PARAMS
    assert "use_htf_trend" in DEFAULT_PARAMS
    assert "ltf_ema_fast" in DEFAULT_PARAMS
    assert "ltf_ema_slow" in DEFAULT_PARAMS
    assert "pullback_ema" in DEFAULT_PARAMS
    assert "atr_sl_mult" in DEFAULT_PARAMS
    assert "atr_tp_mult" in DEFAULT_PARAMS
    assert "adx_threshold" in DEFAULT_PARAMS
    assert "rsi_max_long" in DEFAULT_PARAMS
    assert "rsi_min_short" in DEFAULT_PARAMS
    assert "volume_mult" in DEFAULT_PARAMS
    assert "min_atr_pct" in DEFAULT_PARAMS
    assert "use_rsi_filter" in DEFAULT_PARAMS
    assert "use_volume_filter" in DEFAULT_PARAMS

    assert DEFAULT_PARAMS["htf_ema_fast"] == 20
    assert DEFAULT_PARAMS["htf_ema_slow"] == 50
    assert DEFAULT_PARAMS["use_htf_trend"] is True
    assert DEFAULT_PARAMS["ltf_ema_fast"] == 9
    assert DEFAULT_PARAMS["ltf_ema_slow"] == 21
    assert DEFAULT_PARAMS["pullback_ema"] == 9
    assert DEFAULT_PARAMS["atr_sl_mult"] == 1.5
    assert DEFAULT_PARAMS["atr_tp_mult"] == 4.0
    assert DEFAULT_PARAMS["adx_threshold"] == 20
    assert DEFAULT_PARAMS["rsi_max_long"] == 60
    assert DEFAULT_PARAMS["rsi_min_short"] == 40
    assert DEFAULT_PARAMS["volume_mult"] == 1.2
    assert DEFAULT_PARAMS["min_atr_pct"] == 0.0005


def test_pattern_cols():
    """Test pattern column sets."""
    assert "hammer" in PATTERN_COLS_BULLISH
    assert "bullish_engulfing" in PATTERN_COLS_BULLISH
    assert "double_bottom" in PATTERN_COLS_BULLISH

    assert "bearish_engulfing" in PATTERN_COLS_BEARISH
    assert "double_top" in PATTERN_COLS_BEARISH


def test_vectorized_evaluate():
    """Test vectorized_evaluate function for EMA pullback strategy."""
    n = 10
    ema_fast = np.array([10, 11, 12, 13, 14, 15, 16, 17, 18, 19], dtype=float)
    ema_slow = np.array([9, 10, 11, 12, 13, 14, 15, 16, 17, 18], dtype=float)
    ema_pullback = np.array([9.5, 10.5, 11.5, 12.5, 13.5, 14.5, 15.5, 16.5, 17.5, 18.5], dtype=float)
    htf_trend_up = np.array([True] * n)
    htf_trend_down = np.array([False] * n)
    adx = np.array([25] * n)
    atr = np.array([0.5] * n)
    close = np.array([10, 11, 12, 13, 14, 15, 16, 17, 18, 19], dtype=float)
    rsi = np.array([50] * n)
    volume_ratio = np.array([1.5] * n)

    actions = vectorized_evaluate(
        ema_fast, ema_slow, ema_pullback,
        htf_trend_up, htf_trend_down,
        adx, atr, close, rsi, volume_ratio
    )

    # All should pass initial trend filter (HTF up, LTF up, ADX ok, RSI ok, volume ok)
    # Result should be int8 array with 0=HOLD, 1=BUY, 2=SELL
    assert actions.dtype == np.int8
    assert len(actions) == n
    # With HTF up and LTF uptrend, we expect BUY (1) where valid
    # Since all values are valid and trend aligned, should be BUY
    assert np.all(actions[actions != 0] == 1)  # Only BUY or HOLD


def test_vectorized_evaluate_adx_filter():
    """Test ADX threshold filtering in vectorized_evaluate."""
    n = 5
    ema_fast = np.array([10, 11, 12, 13, 14], dtype=float)
    ema_slow = np.array([9, 10, 11, 12, 13], dtype=float)
    ema_pullback = np.array([9.5, 10.5, 11.5, 12.5, 13.5], dtype=float)
    htf_trend_up = np.array([True] * n)
    htf_trend_down = np.array([False] * n)
    adx = np.array([15, 25, 15, 25, 25])  # Below and above threshold
    atr = np.array([0.5] * n)
    close = np.array([10, 11, 12, 13, 14], dtype=float)
    rsi = np.array([50] * n)
    volume_ratio = np.array([1.5] * n)

    actions = vectorized_evaluate(
        ema_fast, ema_slow, ema_pullback,
        htf_trend_up, htf_trend_down,
        adx, atr, close, rsi, volume_ratio
    )

    # First and third should be HOLD (ADX < 20)
    # Second, fourth, fifth should be BUY (ADX >= 20, trend aligned)
    assert actions[0] == 0  # HOLD
    assert actions[1] == 1  # BUY
    assert actions[2] == 0  # HOLD
    assert actions[3] == 1  # BUY
    assert actions[4] == 1  # BUY


def test_vectorized_evaluate_nan_handling():
    """Test NaN handling in vectorized_evaluate."""
    n = 5
    ema_fast = np.array([10, np.nan, 12, 13, 14], dtype=float)
    ema_slow = np.array([9, 10, 11, 12, 13], dtype=float)
    ema_pullback = np.array([9.5, 10.5, 11.5, 12.5, 13.5], dtype=float)
    htf_trend_up = np.array([True] * n)
    htf_trend_down = np.array([False] * n)
    adx = np.array([25] * n)
    atr = np.array([0.5] * n)
    close = np.array([10, 11, 12, 13, 14], dtype=float)
    rsi = np.array([50] * n)
    volume_ratio = np.array([1.5] * n)

    actions = vectorized_evaluate(
        ema_fast, ema_slow, ema_pullback,
        htf_trend_up, htf_trend_down,
        adx, atr, close, rsi, volume_ratio
    )

    # Middle should be HOLD due to NaN ema_fast
    assert actions[0] == 1  # BUY
    assert actions[1] == 0  # HOLD (NaN)
    assert actions[2] == 1  # BUY
    assert actions[3] == 1  # BUY
    assert actions[4] == 1  # BUY


def test_precompute_pattern_bias_codes():
    """Test precompute_pattern_bias_codes function."""
    # Create minimal prepared dict
    prepared = {
        "length": 10,
        "hammers": np.array([False] * 10),
        "bull_engulf": np.array([False] * 10),
        "bear_engulf": np.array([False] * 10),
        "swings": {"highs": [], "lows": []},
    }

    codes = precompute_pattern_bias_codes(prepared)

    assert len(codes) == 10
    assert codes.dtype == np.int8
    # No patterns, all should be neutral (0)
    assert np.all(codes == 0)