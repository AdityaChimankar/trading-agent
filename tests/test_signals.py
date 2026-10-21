"""Unit tests for core signals module."""
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
    assert LOOKBACK_MIN == 20
    assert FORWARD_WINDOW == 6
    assert SWING_ORDER == 5
    assert NEUTRAL_SENTIMENT == 0.0
    assert SLIPPAGE == 0.0005
    assert MAX_WORKERS == 4


def test_default_params():
    """Test DEFAULT_PARAMS structure."""
    assert "rsi_oversold" in DEFAULT_PARAMS
    assert "rsi_overbought" in DEFAULT_PARAMS
    assert "adx_threshold" in DEFAULT_PARAMS
    assert "pattern_confirm_buy_rsi" in DEFAULT_PARAMS
    assert "pattern_confirm_sell_rsi" in DEFAULT_PARAMS

    assert DEFAULT_PARAMS["rsi_oversold"] == 30
    assert DEFAULT_PARAMS["rsi_overbought"] == 70
    assert DEFAULT_PARAMS["adx_threshold"] == 20


def test_pattern_cols():
    """Test pattern column sets."""
    assert "hammer" in PATTERN_COLS_BULLISH
    assert "bullish_engulfing" in PATTERN_COLS_BULLISH
    assert "double_bottom" in PATTERN_COLS_BULLISH

    assert "bearish_engulfing" in PATTERN_COLS_BEARISH
    assert "double_top" in PATTERN_COLS_BEARISH


def test_vectorized_evaluate():
    """Test vectorized_evaluate function."""
    rsi = np.array([25, 35, 75, 65, 50])
    adx = np.array([25, 25, 25, 25, 25])
    pattern_codes = np.array([1, 1, 2, 2, 0], dtype=np.int8)  # bullish, bullish, bearish, bearish, neutral

    actions = vectorized_evaluate(rsi, adx, pattern_codes)

    # Should be BUY (1) for first two (RSI < 45, bullish pattern, ADX >= 20)
    # Should be SELL (2) for next two (RSI > 55, bearish pattern, ADX >= 20)
    # Should be HOLD (0) for last (neutral pattern)
    assert actions[0] == 1  # BUY
    assert actions[1] == 1  # BUY
    assert actions[2] == 2  # SELL
    assert actions[3] == 2  # SELL
    assert actions[4] == 0  # HOLD


def test_vectorized_evaluate_adx_filter():
    """Test ADX threshold filtering."""
    rsi = np.array([25, 25])
    adx = np.array([15, 25])  # First below threshold, second above
    pattern_codes = np.array([1, 1], dtype=np.int8)

    actions = vectorized_evaluate(rsi, adx, pattern_codes)

    # First should be HOLD (ADX < 20)
    # Second should be BUY (ADX >= 20, RSI < 45, bullish pattern)
    assert actions[0] == 0
    assert actions[1] == 1


def test_vectorized_evaluate_nan_handling():
    """Test NaN handling in vectorized_evaluate."""
    rsi = np.array([25, np.nan, 25])
    adx = np.array([25, 25, 25])
    pattern_codes = np.array([1, 1, 1], dtype=np.int8)

    actions = vectorized_evaluate(rsi, adx, pattern_codes)

    # Middle should be HOLD due to NaN RSI
    assert actions[0] == 1
    assert actions[1] == 0
    assert actions[2] == 1


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