"""Opportunity finder and ranking."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from trading_agent.core.logging_config import get_logger
from trading_agent.storage.parquet_store import get_parquet_store
from trading_agent.strategy.candidates import Candidate, CandidateGenerator

logger = get_logger(__name__)


@dataclass(frozen=True)
class Opportunity:
    """Trading opportunity with score and details."""

    symbol: str
    timestamp: datetime
    score: float
    rank: int
    rule_signal: str
    rule_confidence: float
    ml_signal: str | None
    ml_confidence: float
    llm_signal: str | None
    llm_confidence: float
    risk_reward: float
    atr: float
    volume: int
    price: float
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class OpportunityConfig:
    """Opportunity finder configuration."""

    min_score: float = 0.3
    max_opportunities: int = 20
    min_volume: int = 50000
    min_adx: float = 15.0
    require_ml: bool = False
    ml_weight: float = 0.5
    rule_weight: float = 0.3
    llm_weight: float = 0.2


DEFAULT_OPPORTUNITY_CONFIG = OpportunityConfig()


class OpportunityFinder:
    """Find and rank trading opportunities."""

    def __init__(
        self,
        config: OpportunityConfig = DEFAULT_OPPORTUNITY_CONFIG,
        candidate_generator: CandidateGenerator | None = None,
        parquet_store=None,
    ):
        self.config = config
        self.generator = candidate_generator or CandidateGenerator()
        self.parquet = parquet_store or get_parquet_store()

    def find_opportunities(
        self,
        symbols: list[str],
        interval: str = "5minute",
        portfolio_value: float = 1000000,
    ) -> list[Opportunity]:
        """Find opportunities for a list of symbols."""
        # Generate candidates
        candidates = self.generator.generate_all(symbols, interval, portfolio_value)

        if not candidates:
            return []

        # Score each candidate
        opportunities = []
        for i, cand in enumerate(candidates):
            score = self._calculate_score(cand)
            if score < self.config.min_score:
                continue

            opp = Opportunity(
                symbol=cand.symbol,
                timestamp=cand.timestamp,
                score=score,
                rank=0,  # Will be set after sorting
                rule_signal=cand.rule_action,
                rule_confidence=cand.rule_confidence,
                ml_signal=cand.ml_action,
                ml_confidence=cand.ml_confidence,
                llm_signal=cand.llm_action,
                llm_confidence=cand.llm_confidence,
                risk_reward=self._calculate_risk_reward(cand),
                atr=cand.atr,
                volume=cand.volume,
                price=cand.close,
                metadata=cand.metadata,
            )
            opportunities.append(opp)

        # Sort by score descending
        opportunities.sort(key=lambda o: o.score, reverse=True)

        # Assign ranks
        for i, opp in enumerate(opportunities):
            opp.rank = i + 1

        # Limit
        return opportunities[: self.config.max_opportunities]

    def _calculate_score(self, candidate: Candidate) -> float:
        """Calculate opportunity score."""
        score = 0.0

        # Rule signal contribution
        if candidate.rule_action != "HOLD":
            score += candidate.rule_confidence * self.config.rule_weight

        # ML signal contribution
        if candidate.ml_action and candidate.ml_action != "HOLD":
            score += candidate.ml_confidence * self.config.ml_weight

        # LLM signal contribution
        if candidate.llm_action and candidate.llm_action != "HOLD":
            score += candidate.llm_confidence * self.config.llm_weight

        # Volume bonus
        if candidate.volume > 100000:
            score += 0.05
        elif candidate.volume > 50000:
            score += 0.02

        # ADX bonus
        if candidate.adx > 25:
            score += 0.05
        elif candidate.adx > 20:
            score += 0.02

        # Pattern bonus
        if (
            candidate.rule_action == "BUY"
            and candidate.pattern_bias == 1
            or candidate.rule_action == "SELL"
            and candidate.pattern_bias == 2
        ):
            score += 0.05

        return min(1.0, score)

    def _calculate_risk_reward(self, candidate: Candidate) -> float:
        """Calculate risk-reward ratio."""
        metadata = candidate.metadata
        stop_loss = metadata.get("stop_loss", 0)
        take_profit = metadata.get("take_profit")

        if stop_loss <= 0:
            return 0.0

        risk = abs(candidate.close - stop_loss)
        if take_profit and take_profit > 0:
            reward = abs(take_profit - candidate.close)
        else:
            # Default 2:1 if no take profit
            reward = risk * 2

        return reward / risk if risk > 0 else 0.0

    def get_opportunity_details(self, opportunity: Opportunity) -> dict:
        """Get detailed analysis for an opportunity."""
        # Fetch recent candles for context
        df = self.parquet.read_latest(opportunity.symbol, "5minute", n=50)

        details = {
            "symbol": opportunity.symbol,
            "timestamp": opportunity.timestamp.isoformat(),
            "score": opportunity.score,
            "rank": opportunity.rank,
            "current_price": opportunity.price,
            "signals": {
                "rule": {
                    "action": opportunity.rule_signal,
                    "confidence": opportunity.rule_confidence,
                },
                "ml": {"action": opportunity.ml_signal, "confidence": opportunity.ml_confidence},
                "llm": {"action": opportunity.llm_signal, "confidence": opportunity.llm_confidence},
            },
            "risk_reward": opportunity.risk_reward,
            "atr": opportunity.atr,
            "volume": opportunity.volume,
            "technical": {},
        }

        if not df.empty:
            last = df.iloc[-1]
            details["technical"] = {
                "open": float(last["open"]),
                "high": float(last["high"]),
                "low": float(last["low"]),
                "close": float(last["close"]),
                "volume": int(last["volume"]),
                "change_pct": float((last["close"] - last["open"]) / last["open"] * 100),
            }

        return details
