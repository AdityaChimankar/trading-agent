"""Unit tests for core signals module (Rule-based signal generation)."""
import numpy as np
import pandas as pd

from trading_agent.core.signals import (
    FORWARD_WINDOW,
    LOOKBACK_MIN,
    SWING_ORDER,
    SLIPPAGE,
    MAX_WORKERS,
    SignalParams,
    SignalResult,
    DEFAULT_PARAMS,
    evaluate_signals,
    evaluate_signals_loop,
    prepare_symbol_data,
    prepare_symbol_series,
    simulate_trades,
    summarize,
    verify_fast_path_matches_reference,
)


def test_constants():
    """Test that constants have expected values."""
    assert LOOKBACK_MIN == 30
    assert FORWARD_WINDOW == 12
    assert SWING_ORDER == 5
    assert SLIPPAGE == 0.0005
    assert MAX_WORKERS == 4


def test_signal_params_defaults():
    """Test SignalParams default values."""
    params = SignalParams()
    assert params.rsi_oversold == 30
    assert params.rsi_overbought == 70
    assert params.adx_threshold == 20
    assert params.pattern_confirm_buy_rsi == 45
    assert params.pattern_confirm_sell_rsi == 55
    assert params.sentiment_buy_threshold == 0.3
    assert params.sentiment_sell_threshold == -0.3


def test_default_params():
    """Test DEFAULT_PARAMS is a SignalParams instance."""
    assert isinstance(DEFAULT_PARAMS, SignalParams)
    assert DEFAULT_PARAMS.rsi_oversold == 30
    assert DEFAULT_PARAMS.adx_threshold == 20


def test_evaluate_signals():
    """Test evaluate_signals function (vectorized)."""
    n = 10
    rsi = np.array([30, 40, 50, 60, 70, 30, 40, 50, 60, 70], dtype=np.float64)
    adx = np.array([25] * n, dtype=np.float64)
    pattern_bias = np.array([1, 1, 1, 1, 1, 2, 2, 2, 2, 2], dtype=np.int8)  # bullish, bearish
    sentiment = np.array([0.0] * n, dtype=np.float64)  # Neutral

    actions = evaluate_signals(rsi, adx, pattern_bias, sentiment)

    assert actions.dtype == np.int8
    assert len(actions) == n
    # With neutral sentiment, bullish pattern and rsi < 45 -> BUY
    # With neutral sentiment, bearish pattern and rsi > 55 -> SELL
    assert actions[0] == 1  # BUY (rsi=30 < 45, bullish pattern)
    assert actions[1] == 1  # BUY (rsi=40 < 45, bullish pattern)
    assert actions[4] == 0  # HOLD (rsi=70 > 45, bullish pattern)
    # For bearish pattern: need rsi > 55
    assert actions[5] == 0  # HOLD (rsi=30 <= 55, bearish pattern)
    assert actions[8] == 2  # SELL (rsi=60 > 55, bearish pattern)
    assert actions[9] == 2  # SELL (rsi=70 > 55, bearish pattern)


def test_evaluate_signals_adx_filter():
    """Test ADX threshold filtering in evaluate_signals."""
    n = 5
    rsi = np.array([30, 30, 30, 30, 30], dtype=np.float64)
    adx = np.array([15, 25, 15, 25, 25], dtype=np.float64)  # Below and above threshold
    pattern_bias = np.array([1, 1, 1, 1, 1], dtype=np.int8)  # All bullish
    sentiment = np.array([0.0] * n, dtype=np.float64)

    actions = evaluate_signals(rsi, adx, pattern_bias, sentiment)

    # First and third should be HOLD (ADX < 20)
    assert actions[0] == 0  # HOLD
    assert actions[1] == 1  # BUY
    assert actions[2] == 0  # HOLD
    assert actions[3] == 1  # BUY
    assert actions[4] == 1  # BUY


def test_evaluate_signals_nan_handling():
    """Test NaN handling in evaluate_signals."""
    n = 5
    rsi = np.array([30, np.nan, 30, 30, 30], dtype=np.float64)
    adx = np.array([25] * n, dtype=np.float64)
    pattern_bias = np.array([1, 1, 1, 1, 1], dtype=np.int8)
    sentiment = np.array([0.0] * n, dtype=np.float64)

    actions = evaluate_signals(rsi, adx, pattern_bias, sentiment)

    # Middle should be HOLD due to NaN rsi
    assert actions[0] == 1  # BUY
    assert actions[1] == 0  # HOLD (NaN)
    assert actions[2] == 1  # BUY
    assert actions[3] == 1  # BUY
    assert actions[4] == 1  # BUY


def test_evaluate_signals_sentiment_buy():
    """Test sentiment-driven BUY signals."""
    n = 3
    rsi = np.array([25, 25, 25], dtype=np.float64)  # Oversold
    adx = np.array([25, 25, 25], dtype=np.float64)
    pattern_bias = np.array([0, 0, 0], dtype=np.int8)  # No pattern
    sentiment = np.array([0.5, 0.5, 0.5], dtype=np.float64)  # Bullish sentiment > 0.3

    actions = evaluate_signals(rsi, adx, pattern_bias, sentiment)

    # Should be BUY due to bullish sentiment + oversold RSI
    assert np.all(actions == 1)


def test_evaluate_signals_sentiment_sell():
    """Test sentiment-driven SELL signals."""
    n = 3
    rsi = np.array([75, 75, 75], dtype=np.float64)  # Overbought
    adx = np.array([25, 25, 25], dtype=np.float64)
    pattern_bias = np.array([0, 0, 0], dtype=np.int8)  # No pattern
    sentiment = np.array([-0.5, -0.5, -0.5], dtype=np.float64)  # Bearish sentiment < -0.3

    actions = evaluate_signals(rsi, adx, pattern_bias, sentiment)

    # Should be SELL due to bearish sentiment + overbought RSI
    assert np.all(actions == 2)


def test_prepare_symbol_data():
    """Test prepare_symbol_data function."""
    # Create sample DataFrame
    df = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=100, freq="5min"),
        "open": np.random.uniform(100, 200, 100),
        "high": np.random.uniform(150, 250, 100),
        "low": np.random.uniform(50, 150, 100),
        "close": np.random.uniform(100, 200, 100),
        "volume": np.random.randint(1000, 10000, 100),
    })
    df.attrs["symbol"] = "TEST"

    prepared = prepare_symbol_data(df)

    assert "rsis" in prepared
    assert "adxs" in prepared
    assert "pattern_bias" in prepared
    assert "closes" in prepared
    assert prepared["length"] == 100
    assert prepared["symbol"] == "TEST"


def test_simulate_trades():
    """Test simulate_trades function."""
    # Create prepared data
    n = 50
    prepared = {
        "length": n,
        "rsis": np.array([30] * n, dtype=np.float64),
        "adxs": np.array([25] * n, dtype=np.float64),
        "pattern_bias": np.array([1] * n, dtype=np.int8),
        "timestamps": np.array(pd.date_range("2024-01-01", periods=n, freq="5min")),
        "closes": np.linspace(100, 150, n),
        "opens": np.linspace(99, 149, n),
        "symbol": "TEST",
    }

    # simulate_trades expects a dict for params
    params = {
        "adx_threshold": 20,
        "pattern_confirm_buy_rsi": 45,
        "pattern_confirm_sell_rsi": 55,
    }
    trades = simulate_trades(prepared, params)

    # Should generate some trades
    assert isinstance(trades, list)
    # With bullish pattern and rsi=30, should get BUY signals
    if trades:
        assert trades[0]["action"] in ("BUY", "SELL")
        assert "entry" in trades[0]
        assert "exit" in trades[0]
        assert "return_pct" in trades[0]


def test_summarize():
    """Test summarize function."""
    trades = [
        {"return_pct": 0.02, "exit_reason": "target"},
        {"return_pct": -0.01, "exit_reason": "stop"},
        {"return_pct": 0.03, "exit_reason": "target"},
        {"return_pct": -0.02, "exit_reason": "stop"},
    ]

    result = summarize("TEST", trades)

    assert result["label"] == "TEST"
    assert result["trades"] == 4
    assert result["win_rate"] == 50.0
    assert "avg_win_pct" in result
    assert "avg_loss_pct" in result
    assert "risk_reward_ratio" in result
    assert "sharpe_like_ratio" in result
    assert "max_drawdown_pct" in result
    assert "total_return_pct" in result


def test_verify_fast_path_matches_reference():
    """Test verify_fast_path_matches_reference function."""
    # This function may return False if no data, but shouldn't error
    result = verify_fast_path_matches_reference("NONEXISTENT")
    assert result is False