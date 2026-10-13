"""Tests for risk management modules."""

import pytest
import numpy as np
from datetime import datetime, timezone

from trading_agent.risk.portfolio_risk import (
    RiskLimits, PositionRisk, PortfolioRisk, PortfolioRiskManager, DEFAULT_RISK_LIMITS,
)
from trading_agent.risk.position_monitor import (
    PositionStatus, MonitorConfig, PositionState, PositionMonitor, DEFAULT_MONITOR_CONFIG,
)
from trading_agent.risk.wallet import (
    WalletConfig, TradeRecord, WalletSnapshot, PaperWallet, DEFAULT_WALLET_CONFIG,
)
from trading_agent.core.position_sizing import PositionSizingConfig


class TestPortfolioRisk:
    """Test portfolio risk management."""

    def setup_method(self):
        self.limits = RiskLimits(
            max_portfolio_risk_pct=0.06,
            max_single_position_pct=0.10,
            max_positions=5,
        )
        self.manager = PortfolioRiskManager(limits=self.limits)
        self.manager.set_capital(1000000, 1000000)
        self.manager.set_sector_map({"RELIANCE": "ENERGY", "TCS": "TECH"})

    def test_add_position_valid(self):
        valid, reason = self.manager.add_position(
            symbol="RELIANCE",
            action="BUY",
            entry_price=2500,
            quantity=40,
            stop_loss=2400,
            take_profit=2700,
            atr=50,
            atr_percentile=0.5,
            sector="ENERGY",
        )
        assert valid
        assert reason == "OK"
        assert "RELIANCE" in self.manager.positions

    def test_add_position_exceeds_risk(self):
        # Add first position
        self.manager.add_position("RELIANCE", "BUY", 2500, 40, 2400, 2700, 50, 0.5, "ENERGY")

        # Try to add position that exceeds portfolio risk
        valid, reason = self.manager.add_position(
            symbol="TCS",
            action="BUY",
            entry_price=3500,
            quantity=100,  # Large position
            stop_loss=3300,
            take_profit=3900,
            atr=80,
            atr_percentile=0.5,
            sector="TECH",
        )
        assert not valid
        assert "portfolio limit" in reason.lower() or "risk" in reason.lower()

    def test_get_position_size(self):
        size = self.manager.get_position_size(
            symbol="RELIANCE",
            entry_price=2500,
            atr=50,
            atr_percentile=0.5,
        )
        assert size > 0

    def test_check_risk_limits(self):
        self.manager.add_position("RELIANCE", "BUY", 2500, 40, 2400, 2700, 50, 0.5, "ENERGY")
        risk = self.manager.check_risk_limits()

        assert risk.total_value > 0
        assert risk.total_risk_pct >= 0
        assert isinstance(risk.limit_breaches, list)


class TestPositionMonitor:
    """Test position monitoring."""

    def setup_method(self):
        self.config = MonitorConfig()
        self.monitor = PositionMonitor(config=self.config)

    def test_update_price(self):
        from trading_agent.storage.db import Position
        from trading_agent.risk.position_monitor import PositionState, PositionStatus
        position = Position(
            id=1,
            symbol="RELIANCE",
            action="BUY",
            entry_price=2500,
            entry_time=datetime.now(timezone.utc),
            quantity=40,
            stop_loss=2400,
            take_profit=2700,
            strategy="test",
            status="OPEN",
        )

        # Add position to monitor state
        self.monitor._states["RELIANCE"] = PositionState(
            position=position,
            current_price=2500,
            unrealized_pnl=0.0,
            unrealized_pnl_pct=0.0,
            highest_price=2500,
            lowest_price=2500,
            trailing_stop=2400,
            partial_exits=[],
            minutes_held=0,
            status=PositionStatus.OPEN,
        )

        state = self.monitor.update_price("RELIANCE", 2550, 2560, 2540)
        assert state is not None
        assert state.current_price == 2550
        assert state.highest_price == 2560
        assert state.unrealized_pnl > 0

    def test_trailing_stop(self):
        from trading_agent.storage.db import Position
        from trading_agent.risk.position_monitor import PositionState, PositionStatus
        position = Position(
            id=1,
            symbol="RELIANCE",
            action="BUY",
            entry_price=2500,
            entry_time=datetime.now(timezone.utc),
            quantity=40,
            stop_loss=2400,
            take_profit=2700,
            strategy="test",
            status="OPEN",
        )

        # Add position to monitor state
        self.monitor._states["RELIANCE"] = PositionState(
            position=position,
            current_price=2500,
            unrealized_pnl=0.0,
            unrealized_pnl_pct=0.0,
            highest_price=2500,
            lowest_price=2500,
            trailing_stop=2400,
            partial_exits=[],
            minutes_held=0,
            status=PositionStatus.OPEN,
        )

        # Price moves up
        self.monitor.update_price("RELIANCE", 2550)
        self.monitor.update_price("RELIANCE", 2600)
        state = self.monitor._states["RELIANCE"]

        # Trailing stop should have moved up
        assert state.trailing_stop > 2400

    def test_check_exits_stop_loss(self):
        from trading_agent.storage.db import Position
        from trading_agent.risk.position_monitor import PositionState, PositionStatus
        position = Position(
            id=1,
            symbol="RELIANCE",
            action="BUY",
            entry_price=2500,
            entry_time=datetime.now(timezone.utc),
            quantity=40,
            stop_loss=2400,
            take_profit=2700,
            strategy="test",
            status="OPEN",
        )

        # Add position to monitor state
        self.monitor._states["RELIANCE"] = PositionState(
            position=position,
            current_price=2500,
            unrealized_pnl=0.0,
            unrealized_pnl_pct=0.0,
            highest_price=2500,
            lowest_price=2500,
            trailing_stop=2400,
            partial_exits=[],
            minutes_held=0,
            status=PositionStatus.OPEN,
        )

        self.monitor.update_price("RELIANCE", 2390)  # Below stop loss
        exits = self.monitor.check_exits("RELIANCE")

        assert len(exits) > 0
        assert exits[0]["reason"] == "stop_loss"


class TestPaperWallet:
    """Test paper trading wallet."""

    def setup_method(self):
        self.wallet = PaperWallet(initial_cash=1000000)

    def test_buy(self):
        success, msg = self.wallet.buy("RELIANCE", 2500, 40)
        assert success
        assert self.wallet.cash < 1000000
        assert self.wallet.invested > 0
        assert "RELIANCE" in self.wallet.open_positions

    def test_sell(self):
        self.wallet.buy("RELIANCE", 2500, 40)
        success, msg = self.wallet.sell("RELIANCE", 2600, 40)
        assert success
        assert self.wallet.cash > 1000000  # Profit
        assert "RELIANCE" not in self.wallet.open_positions

    def test_sell_partial(self):
        self.wallet.buy("RELIANCE", 2500, 40)
        success, msg = self.wallet.sell("RELIANCE", 2600, 20)
        assert success
        assert self.wallet.open_positions["RELIANCE"]["quantity"] == 20

    def test_insufficient_cash(self):
        success, msg = self.wallet.buy("RELIANCE", 2500, 1000)  # 25L > 10L
        assert not success
        assert "insufficient" in msg.lower()

    def test_short_sell(self):
        success, msg = self.wallet.short_sell("RELIANCE", 2500, 40)
        assert success
        assert self.wallet.open_positions["RELIANCE"]["action"] == "SELL"

    def test_cover_short(self):
        self.wallet.short_sell("RELIANCE", 2500, 40)
        success, msg = self.wallet.cover_short("RELIANCE", 2400, 40)
        assert success
        assert "RELIANCE" not in self.wallet.open_positions

    def test_mark_to_market(self):
        self.wallet.buy("RELIANCE", 2500, 40)
        unrealized = self.wallet.mark_to_market({"RELIANCE": 2600})
        assert unrealized == 4000  # (2600-2500) * 40

    def test_get_total_value(self):
        self.wallet.buy("RELIANCE", 2500, 40)
        total = self.wallet.get_total_value({"RELIANCE": 2600})
        assert total > 1000000

    def test_performance_metrics(self):
        self.wallet.buy("RELIANCE", 2500, 40)
        self.wallet.sell("RELIANCE", 2600, 40)
        metrics = self.wallet.get_performance_metrics()

        assert metrics["total_trades"] == 1
        assert metrics["winning_trades"] == 1
        assert metrics["total_pnl"] > 0