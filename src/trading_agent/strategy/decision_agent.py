"""Rule-based decision agent."""

from __future__ import annotations

from dataclasses import dataclass

from trading_agent.core.logging_config import get_logger
from trading_agent.core.signals import SignalParams
from trading_agent.strategy.candidates import Candidate, CandidateGenerator

logger = get_logger(__name__)


@dataclass(frozen=True)
class RuleDecision:
    """Rule-based decision output."""

    action: str  # BUY, SELL, HOLD
    confidence: float
    reasoning: str
    rsi: float
    adx: float
    pattern_bias: int
    params_used: SignalParams


class RuleDecisionAgent:
    """Rule-based trading decision agent."""

    def __init__(
        self,
        params: SignalParams | None = None,
        candidate_generator: CandidateGenerator | None = None,
    ):
        self.params = params or SignalParams()
        self.generator = candidate_generator or CandidateGenerator()

    def decide(self, candidate: Candidate) -> RuleDecision:
        """Make a rule-based decision for a candidate."""
        # Re-evaluate with current params for latest data
        # (In practice, candidate already has rule_action from generator)

        reasoning_parts = []

        if candidate.rule_action == "HOLD":
            reasoning = "No rule signal triggered"
            confidence = 0.0
        else:
            reasoning_parts.append(f"RSI: {candidate.rsi:.1f}")
            reasoning_parts.append(
                f"ADX: {candidate.adx:.1f} (threshold: {self.params.adx_threshold})"
            )
            reasoning_parts.append(
                f"Pattern: {'Bullish' if candidate.pattern_bias == 1 else 'Bearish' if candidate.pattern_bias == 2 else 'Neutral'}"
            )

            if candidate.rule_action == "BUY":
                reasoning_parts.append(
                    f"RSI < {self.params.pattern_confirm_buy_rsi} (pattern confirmed BUY)"
                )
            else:
                reasoning_parts.append(
                    f"RSI > {self.params.pattern_confirm_sell_rsi} (pattern confirmed SELL)"
                )

            reasoning = "; ".join(reasoning_parts)
            confidence = candidate.rule_confidence

        return RuleDecision(
            action=candidate.rule_action,
            confidence=confidence,
            reasoning=reasoning,
            rsi=candidate.rsi,
            adx=candidate.adx,
            pattern_bias=candidate.pattern_bias,
            params_used=self.params,
        )

    def decide_batch(self, candidates: list[Candidate]) -> list[RuleDecision]:
        """Make decisions for multiple candidates."""
        return [self.decide(c) for c in candidates]

    def get_params(self) -> SignalParams:
        """Get current parameters."""
        return self.params

    def update_params(self, params: SignalParams) -> None:
        """Update decision parameters."""
        self.params = params
        # Update generator config too
        self.generator.config.signal_params = params
