"""Tests for analysis modules."""

import pytest
import numpy as np
import pandas as pd
from datetime import datetime, timezone

from trading_agent.analysis.opportunity_finder import (
    Opportunity, OpportunityConfig, OpportunityFinder, DEFAULT_OPPORTUNITY_CONFIG,
)
from trading_agent.analysis.rankings import (
    RankedSymbol, rank_by_momentum, rank_by_mean_reversion, combine_rankings,
)
from trading_agent.analysis.technical_summary import (
    TechnicalSummary, TechnicalAnalyzer, generate_summary_text,
)
from trading_agent.analysis.sentiment import (
    SentimentLabel, SentimentScore, SentimentAnalyzer,
)
from trading_agent.strategy.candidates import Candidate


class TestOpportunityFinder:
    """Test opportunity finder."""

    def setup_method(self):
        self.config = OpportunityConfig()
        self.finder = OpportunityFinder(config=self.config)

    def test_opportunity_creation(self):
        opp = Opportunity(
            symbol="RELIANCE",
            timestamp=datetime.now(timezone.utc),
            score=0.75,
            rank=1,
            rule_signal="BUY",
            rule_confidence=0.8,
            ml_signal="BUY",
            ml_confidence=0.7,
            llm_signal=None,
            llm_confidence=0.0,
            risk_reward=2.5,
            atr=45.0,
            volume=50000,
            price=2500.0,
        )
        assert opp.symbol == "RELIANCE"
        assert opp.score == 0.75

    def test_calculate_score(self):
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
            ml_action="BUY",
            ml_confidence=0.65,
            volume=50000,
        )
        score = self.finder._calculate_score(candidate)
        assert 0 <= score <= 1.0

    def test_calculate_risk_reward(self):
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
            metadata={"stop_loss": 2400, "take_profit": 2700},
            volume=50000,
        )
        rr = self.finder._calculate_risk_reward(candidate)
        assert rr > 0


class TestRankings:
    """Test ranking functions."""

    def test_rank_by_momentum(self):
        returns = {"RELIANCE": 0.05, "TCS": 0.03, "INFY": -0.02, "HDFC": 0.04}
        ranked = rank_by_momentum(["RELIANCE", "TCS", "INFY", "HDFC"], returns, top_n=3)

        assert len(ranked) == 3
        assert ranked[0].symbol == "RELIANCE"
        assert ranked[1].symbol == "HDFC"
        assert ranked[2].symbol == "TCS"

    def test_rank_by_mean_reversion(self):
        rsi = {"RELIANCE": 25, "TCS": 75, "INFY": 50}
        bb_pos = {"RELIANCE": 0.1, "TCS": 0.9, "INFY": 0.5}
        ranked = rank_by_mean_reversion(["RELIANCE", "TCS", "INFY"], rsi, bb_pos)

        assert len(ranked) == 2  # Only oversold/overbought
        assert ranked[0].category in ["oversold", "overbought"]

    def test_combine_rankings(self):
        r1 = [RankedSymbol("A", 0.8, 1, "mom", {}, {}), RankedSymbol("B", 0.6, 2, "mom", {}, {})]
        r2 = [RankedSymbol("B", 0.9, 1, "mr", {}, {}), RankedSymbol("A", 0.5, 2, "mr", {}, {})]
        combined = combine_rankings(r1, r2, weights=[0.6, 0.4])

        assert len(combined) == 2
        # B should rank higher due to higher MR score
        assert combined[0].symbol == "B"