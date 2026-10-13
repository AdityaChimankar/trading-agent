"""Fast hybrid strategy for real-time inference."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from trading_agent.core.logging_config import get_logger
from trading_agent.strategy.candidates import Candidate
from trading_agent.strategy.decision_agent import RuleDecisionAgent
from trading_agent.strategy.ml_decision_agent import MLDecisionAgent

logger = get_logger(__name__)


@dataclass(frozen=True)
class FastHybridConfig:
    """Configuration for fast hybrid strategy."""

    # Only use rule + ML (no LLM for speed)
    ml_weight: float = 0.7
    rule_weight: float = 0.3

    # Fast thresholds
    ml_confidence_threshold: float = 0.6
    rule_confidence_threshold: float = 0.4

    # Pre-computed features cache
    use_cached_features: bool = True

    # Decision caching
    cache_decisions: bool = True
    cache_ttl_seconds: int = 300  # 5 minutes


DEFAULT_FAST_CONFIG = FastHybridConfig()


@dataclass
class FastDecision:
    """Fast hybrid decision."""

    symbol: str
    timestamp: pd.Timestamp
    action: str
    confidence: float
    ml_prob_up: float
    ml_prob_down: float
    rule_action: str
    rule_confidence: float
    position_size: int
    stop_loss: float
    take_profit: float | None
    metadata: dict


class FastHybridStrategy:
    """Fast hybrid strategy optimized for real-time execution."""

    def __init__(
        self,
        config: FastHybridConfig = DEFAULT_FAST_CONFIG,
        rule_agent: RuleDecisionAgent | None = None,
        ml_agent: MLDecisionAgent | None = None,
    ):
        self.config = config
        self.rule_agent = rule_agent or RuleDecisionAgent()
        self.ml_agent = ml_agent

        # Decision cache
        self._decision_cache: dict[str, FastDecision] = {}
        self._cache_timestamps: dict[str, pd.Timestamp] = {}

        # Feature cache
        self._feature_cache: dict[str, pd.DataFrame] = {}

    def _get_cache_key(self, candidate: Candidate) -> str:
        """Generate cache key for candidate."""
        return f"{candidate.symbol}_{candidate.timestamp.isoformat()}_{candidate.close:.2f}"

    def _is_cache_valid(self, key: str) -> bool:
        """Check if cached decision is still valid."""
        if key not in self._cache_timestamps:
            return False
        age = (pd.Timestamp.now(tz="UTC") - self._cache_timestamps[key]).total_seconds()
        return age < self.config.cache_ttl_seconds

    def _prepare_features_fast(self, candidate: Candidate) -> pd.DataFrame:
        """Fast feature preparation using cached data."""
        # In practice, this would fetch recent candles and compute features
        # For now, return minimal features
        cache_key = candidate.symbol

        if self.config.use_cached_features and cache_key in self._feature_cache:
            cached = self._feature_cache[cache_key]
            if len(cached) > 0:
                return cached.iloc[[-1]]  # Return latest row

        # Create minimal feature row
        df = pd.DataFrame(
            [
                {
                    "close": candidate.close,
                    "rsi_14": candidate.rsi,
                    "adx_14": candidate.adx,
                    "atr_14": candidate.atr,
                    "pattern_bias": candidate.pattern_bias,
                    "volume": candidate.volume,
                    "return_1": 0.0,  # Would need previous close
                    "return_5": 0.0,
                    "hour_sin": 0.0,
                    "hour_cos": 0.0,
                }
            ]
        )

        return df

    def decide(self, candidate: Candidate) -> FastDecision:
        """Make fast hybrid decision."""
        cache_key = self._get_cache_key(candidate)

        # Check cache
        if self.config.cache_decisions and self._is_cache_valid(cache_key):
            return self._decision_cache[cache_key]

        # Get rule decision (fast)
        rule_dec = self.rule_agent.decide(candidate)

        # Get ML decision (if available)
        ml_prob_up = 0.5
        ml_prob_down = 0.5
        ml_action = "HOLD"
        ml_confidence = 0.0

        if self.ml_agent and self.ml_agent.is_loaded:
            features = self._prepare_features_fast(candidate)
            ml_dec = self.ml_agent.decide(candidate, features)
            ml_action = ml_dec.action
            ml_confidence = ml_dec.confidence
            ml_prob_up = ml_dec.probability_up
            ml_prob_down = ml_dec.probability_down

        # Combine
        action_scores = {"BUY": 0.0, "SELL": 0.0, "HOLD": 0.0}

        if rule_dec.confidence >= self.config.rule_confidence_threshold:
            action_scores[rule_dec.action] += self.config.rule_weight

        if ml_confidence >= self.config.ml_confidence_threshold:
            action_scores[ml_action] += self.config.ml_weight

        final_action = max(action_scores, key=action_scores.get)
        final_confidence = action_scores[final_action]

        # Default to HOLD if no clear signal
        if final_confidence < 0.3:
            final_action = "HOLD"
            final_confidence = 0.0

        # Position sizing (simplified)
        from trading_agent.core.position_sizing import PositionSizingConfig, calculate_position_size

        position = calculate_position_size(
            entry_price=candidate.close,
            atr_value=candidate.atr,
            portfolio_value=1000000,  # Would come from risk manager
            config=PositionSizingConfig(),
        )

        decision = FastDecision(
            symbol=candidate.symbol,
            timestamp=candidate.timestamp,
            action=final_action,
            confidence=final_confidence,
            ml_prob_up=ml_prob_up,
            ml_prob_down=ml_prob_down,
            rule_action=rule_dec.action,
            rule_confidence=rule_dec.confidence,
            position_size=position.shares,
            stop_loss=position.stop_price,
            take_profit=position.take_profit_price,
            metadata={
                "action_scores": action_scores,
                "cache_hit": False,
            },
        )

        # Cache decision
        if self.config.cache_decisions:
            self._decision_cache[cache_key] = decision
            self._cache_timestamps[cache_key] = pd.Timestamp.now(tz="UTC")

        return decision

    def decide_batch(self, candidates: list[Candidate]) -> list[FastDecision]:
        """Make fast decisions for multiple candidates."""
        return [self.decide(c) for c in candidates]

    def clear_cache(self) -> None:
        """Clear decision and feature caches."""
        self._decision_cache.clear()
        self._cache_timestamps.clear()
        self._feature_cache.clear()

    def update_feature_cache(self, symbol: str, features: pd.DataFrame) -> None:
        """Update feature cache for a symbol."""
        self._feature_cache[symbol] = features
