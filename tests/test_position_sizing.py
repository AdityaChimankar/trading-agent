"""Unit tests for position sizing."""
from unittest.mock import patch, MagicMock

import pytest

from core.position_sizing import (
    PositionPlan,
    round_to_tick,
    volatility_risk_multiplier,
    calculate_position,
)


def test_round_to_tick():
    """Test tick size rounding."""
    assert round_to_tick(100.02) == 100.0
    assert round_to_tick(100.03) == 100.05
    assert round_to_tick(100.07) == 100.05
    assert round_to_tick(100.08) == 100.10


def test_volatility_risk_multiplier():
    """Test volatility risk multiplier calculation."""
    # Below cutoff - should be 1.0
    assert volatility_risk_multiplier(0.5) == 1.0
    assert volatility_risk_multiplier(0.79) == 1.0
    assert volatility_risk_multiplier(0.8) == 1.0

    # At cutoff - should be 1.0
    assert volatility_risk_multiplier(0.8) == 1.0

    # Above cutoff - should scale down
    multiplier = volatility_risk_multiplier(0.9)
    assert 0.5 < multiplier < 1.0

    # At 1.0 - should be at floor
    assert volatility_risk_multiplier(1.0) == 0.5

    # None/NaN - should be 1.0
    assert volatility_risk_multiplier(None) == 1.0
    assert volatility_risk_multiplier(float('nan')) == 1.0


def test_position_plan_creation():
    """Test PositionPlan dataclass."""
    plan = PositionPlan(
        symbol="RELIANCE",
        action="BUY",
        entry_price=2500.0,
        atr=25.0,
        stop_loss=2462.5,
        take_profit=2537.5,
        stop_distance=37.5,
        risk_amount=1000.0,
        position_size=26,
        position_value=65000.0,
        capped_by_max_position=False,
        volatility_pctile=0.5,
        risk_multiplier=1.0,
    )

    assert plan.symbol == "RELIANCE"
    assert plan.action == "BUY"
    assert plan.take_profit == 2537.5


@patch("core.position_sizing.latest_indicators")
def test_calculate_position_hold(mock_latest_indicators):
    """Test calculate_position returns None for HOLD."""
    result = calculate_position("RELIANCE", "HOLD", 100000)
    assert result is None


@patch("core.position_sizing.latest_indicators")
def test_calculate_position_error(mock_latest_indicators):
    """Test calculate_position returns None on error."""
    mock_latest_indicators.return_value = {"error": "not enough candles"}

    result = calculate_position("RELIANCE", "BUY", 100000)
    assert result is None


@patch("core.position_sizing.latest_indicators")
@patch("core.position_sizing.current_volatility_percentile")
def test_calculate_position_buy(mock_vol_pctile, mock_latest_indicators):
    """Test calculate_position for BUY action."""
    mock_latest_indicators.return_value = {
        "close": 2500.0,
        "atr": 25.0,
        "rsi": 30,
        "adx": 25,
    }
    mock_vol_pctile.return_value = 0.5

    result = calculate_position("RELIANCE", "BUY", 100000)

    assert result is not None
    assert result.symbol == "RELIANCE"
    assert result.action == "BUY"
    assert result.entry_price == 2500.0
    assert result.atr == 25.0
    assert result.stop_loss < result.entry_price  # Stop below entry for BUY
    assert result.position_size > 0
    assert result.position_value > 0


@patch("core.position_sizing.latest_indicators")
@patch("core.position_sizing.current_volatility_percentile")
def test_calculate_position_sell(mock_vol_pctile, mock_latest_indicators):
    """Test calculate_position for SELL action."""
    mock_latest_indicators.return_value = {
        "close": 2500.0,
        "atr": 25.0,
        "rsi": 70,
        "adx": 25,
    }
    mock_vol_pctile.return_value = 0.5

    result = calculate_position("RELIANCE", "SELL", 100000)

    assert result is not None
    assert result.action == "SELL"
    assert result.stop_loss > result.entry_price  # Stop above entry for SELL


@patch("core.position_sizing.latest_indicators")
@patch("core.position_sizing.current_volatility_percentile")
def test_calculate_position_take_profit_disabled(mock_vol_pctile, mock_latest_indicators):
    """Test calculate_position with USE_TAKE_PROFIT=False."""
    mock_latest_indicators.return_value = {
        "close": 2500.0,
        "atr": 25.0,
        "rsi": 30,
        "adx": 25,
    }
    mock_vol_pctile.return_value = 0.5

    result = calculate_position("RELIANCE", "BUY", 100000, use_take_profit=False)

    assert result is not None
    assert result.take_profit is None


@patch("core.position_sizing.latest_indicators")
@patch("core.position_sizing.current_volatility_percentile")
def test_calculate_position_take_profit_enabled(mock_vol_pctile, mock_latest_indicators):
    """Test calculate_position with USE_TAKE_PROFIT=True."""
    mock_latest_indicators.return_value = {
        "close": 2500.0,
        "atr": 25.0,
        "rsi": 30,
        "adx": 25,
    }
    mock_vol_pctile.return_value = 0.5

    result = calculate_position("RELIANCE", "BUY", 100000, use_take_profit=True)

    assert result is not None
    assert result.take_profit is not None
    assert result.take_profit > result.entry_price