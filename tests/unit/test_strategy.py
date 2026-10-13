"""Tests for strategy modules."""

import pytest
import numpy as np
import pandas as pd
from datetime import datetime, timezone

from trading_agent.strategy.candidates import (
    Candidate, CandidateConfig, CandidateGenerator, generate_candidates,
)
from trading_agent.strategy.decision_agent import RuleDecisionAgent, RuleDecision
from trading_agent.strategy.ml_features import (
    FeatureConfig, prepare_features, add_session_features, add_technical_features,
)
from trading_agent.core.signals import SignalParams


class TestCandidates:
    """Test candidate generation."""

    def test_candidate_creation(self):
        candidate = Candidate(
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
        assert candidate.symbol == "RELIANCE"
        assert candidate.rule_action == "BUY"

    def test_candidate_config_defaults(self):
        config = CandidateConfig()
        assert config.rsi_period == 14
        assert config.adx_period == 14
        assert config.min_adx == 15.0


class TestRuleDecisionAgent:
    """Test rule-based decision agent."""

    def setup_method(self):
        self.agent = RuleDecisionAgent()
        self.candidate = Candidate(
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

    def test_decide_buy(self):
        decision = self.agent.decide(self.candidate)
        assert decision.action == "BUY"
        assert decision.confidence > 0
        assert "RSI" in decision.reasoning

    def test_decide_hold(self):
        hold_candidate = Candidate(
            symbol="RELIANCE",
            timestamp=datetime.now(timezone.utc),
            interval="5minute",
            close=2500.0,
            rsi=50.0,
            adx=15.0,
            atr=45.0,
            pattern_bias=0,
            rule_action="HOLD",
            rule_confidence=0.0,
            volume=50000,
        )
        decision = self.agent.decide(hold_candidate)
        assert decision.action == "HOLD"
        assert decision.confidence == 0.0


class TestMLFeatures:
    """Test ML feature engineering."""

    def setup_method(self):
        np.random.seed(42)
        n = 200
        dates = pd.date_range("2024-01-01", periods=n, freq="5min", tz="UTC")
        close = np.cumsum(np.random.randn(n) * 0.5) + 1000
        high = close + np.abs(np.random.randn(n)) * 1
        low = close - np.abs(np.random.randn(n)) * 1
        open_ = np.roll(close, 1)
        open_[0] = close[0]
        volume = np.random.randint(10000, 100000, n)

        self.df = pd.DataFrame({
            "timestamp": dates,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        })

    def test_prepare_features(self):
        config = FeatureConfig()
        X, y = prepare_features(self.df, config)

        assert len(X) > 0
        assert len(y) == len(X)
        assert "rsi_14" in X.columns or "rsi_14_lag1" in X.columns

    def test_add_session_features(self):
        df_with_session = add_session_features(self.df)
        assert "hour" in df_with_session.columns
        assert "is_market_open" in df_with_session.columns
        assert "hour_sin" in df_with_session.columns

    def test_add_technical_features(self):
        config = FeatureConfig()
        df_tech = add_technical_features(self.df, config)
        assert "rsi_14" in df_tech.columns
        assert "macd" in df_tech.columns
        assert "bb_upper" in df_tech.columns

    def test_create_target(self):
        from trading_agent.strategy.ml_features import create_target
        target = create_target(self.df, horizon=12, target_type="direction")
        assert len(target) == len(self.df)
        assert target.isin([0, 1]).all() or target.isna().all()


class TestHybridStrategy:
    """Test hybrid strategy."""

    def test_hybrid_config(self):
        from trading_agent.strategy.hybrid_strategy import HybridConfig
        config = HybridConfig()
        assert config.rule_weight == 0.3
        assert config.ml_weight == 0.5
        assert config.llm_weight == 0.2

    def test_fast_hybrid_config(self):
        from trading_agent.strategy.fast_hybrid_strategy import FastHybridConfig
        config = FastHybridConfig()
        assert config.ml_weight == 0.7
        assert config.rule_weight == 0.3