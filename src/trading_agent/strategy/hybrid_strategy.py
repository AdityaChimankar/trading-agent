"""Hybrid strategy combining rule, LLM, and ML signals."""

from __future__ import annotations

from dataclasses import dataclass, field

from trading_agent.core.logging_config import get_logger
from trading_agent.strategy.candidates import Candidate
from trading_agent.strategy.decision_agent import RuleDecision, RuleDecisionAgent
from trading_agent.strategy.llm_decision_agent import LLMDecision, LLMDecisionAgent
from trading_agent.strategy.ml_decision_agent import MLDecision, MLDecisionAgent

logger = get_logger(__name__)


@dataclass(frozen=True)
class HybridConfig:
    """Configuration for hybrid strategy."""

    # Weights for signal combination
    rule_weight: float = 0.3
    llm_weight: float = 0.2
    ml_weight: float = 0.5

    # Confidence thresholds
    min_rule_confidence: float = 0.4
    min_llm_confidence: float = 0.5
    min_ml_confidence: float = 0.55

    # Agreement requirements
    require_majority: bool = True  # Need at least 2/3 agreement
    allow_ml_override: bool = True  # ML can override if high confidence

    # Position sizing
    base_position_pct: float = 0.02
    max_position_pct: float = 0.10


DEFAULT_HYBRID_CONFIG = HybridConfig()


@dataclass(frozen=True)
class HybridDecision:
    """Combined hybrid decision."""

    action: str  # BUY, SELL, HOLD
    confidence: float
    rule_decision: RuleDecision
    llm_decision: LLMDecision | None
    ml_decision: MLDecision | None
    agreement_score: float  # 0-1, how much agents agree
    reasoning: str
    metadata: dict = field(default_factory=dict)


class HybridStrategy:
    """Hybrid strategy combining multiple decision agents."""

    def __init__(
        self,
        config: HybridConfig = DEFAULT_HYBRID_CONFIG,
        rule_agent: RuleDecisionAgent | None = None,
        llm_agent: LLMDecisionAgent | None = None,
        ml_agent: MLDecisionAgent | None = None,
    ):
        self.config = config
        self.rule_agent = rule_agent or RuleDecisionAgent()
        self.llm_agent = llm_agent
        self.ml_agent = ml_agent

    def decide(self, candidate: Candidate) -> HybridDecision:
        """Make hybrid decision for a candidate."""
        # Get rule decision (always available)
        rule_dec = self.rule_agent.decide(candidate)

        # Get LLM decision (if available)
        llm_dec = None
        if self.llm_agent:
            # Note: LLM agent is async, would need to handle differently
            pass

        # Get ML decision (if available)
        ml_dec = None
        if self.ml_agent and self.ml_agent.is_loaded:
            ml_dec = self.ml_agent.decide(candidate)

        # Combine decisions
        return self._combine_decisions(candidate, rule_dec, llm_dec, ml_dec)

    def _combine_decisions(
        self,
        candidate: Candidate,
        rule_dec: RuleDecision,
        llm_dec: LLMDecision | None,
        ml_dec: MLDecision | None,
    ) -> HybridDecision:
        """Combine decisions from multiple agents."""
        decisions = []
        weights = []

        # Rule decision
        if rule_dec.confidence >= self.config.min_rule_confidence:
            decisions.append(rule_dec.action)
            weights.append(self.config.rule_weight)

        # LLM decision
        if llm_dec and llm_dec.confidence >= self.config.min_llm_confidence:
            decisions.append(llm_dec.action)
            weights.append(self.config.llm_weight)

        # ML decision
        if ml_dec and ml_dec.confidence >= self.config.min_ml_confidence:
            decisions.append(ml_dec.action)
            weights.append(self.config.ml_weight)

        if not decisions:
            return HybridDecision(
                action="HOLD",
                confidence=0.0,
                rule_decision=rule_dec,
                llm_decision=llm_dec,
                ml_decision=ml_dec,
                agreement_score=0.0,
                reasoning="No agent met confidence threshold",
            )

        # Weighted voting
        action_scores = {"BUY": 0.0, "SELL": 0.0, "HOLD": 0.0}
        for action, weight in zip(decisions, weights, strict=False):
            action_scores[action] += weight

        # Normalize
        total_weight = sum(weights)
        for action in action_scores:
            action_scores[action] /= total_weight

        # Determine final action
        final_action = max(action_scores, key=action_scores.get)
        final_confidence = action_scores[final_action]

        # Check agreement
        unique_actions = set(decisions)
        agreement_score = (
            1.0 - (len(unique_actions) - 1) / 2
        )  # 1.0 if all agree, 0.5 if 2/3, 0 if all differ

        # ML override check
        if (
            self.config.allow_ml_override
            and ml_dec
            and ml_dec.confidence >= 0.75
            and ml_dec.action != final_action
        ):
            final_action = ml_dec.action
            final_confidence = ml_dec.confidence
            agreement_score = 0.5  # Reduced due to override

        # Require majority
        if self.config.require_majority and agreement_score < 0.5:
            final_action = "HOLD"
            final_confidence = 0.0

        # Build reasoning
        reasoning_parts = [
            f"Rule: {rule_dec.action} ({rule_dec.confidence:.2f})",
        ]
        if llm_dec:
            reasoning_parts.append(f"LLM: {llm_dec.action} ({llm_dec.confidence:.2f})")
        if ml_dec:
            reasoning_parts.append(f"ML: {ml_dec.action} ({ml_dec.confidence:.2f})")

        reasoning = f"Combined: {final_action} ({final_confidence:.2f}) | " + " | ".join(
            reasoning_parts
        )

        return HybridDecision(
            action=final_action,
            confidence=final_confidence,
            rule_decision=rule_dec,
            llm_decision=llm_dec,
            ml_decision=ml_dec,
            agreement_score=agreement_score,
            reasoning=reasoning,
            metadata={
                "action_scores": action_scores,
                "weights_used": weights,
            },
        )

    def decide_batch(self, candidates: list[Candidate]) -> list[HybridDecision]:
        """Make decisions for multiple candidates."""
        return [self.decide(c) for c in candidates]
