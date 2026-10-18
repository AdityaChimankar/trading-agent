"""Rule-based decision agent."""

from __future__ import annotations

from dataclasses import dataclass

from trading_agent.core.logging_config import get_logger
from trading_agent.core.signals import SignalParams
from trading_agent.storage.parquet_store import get_parquet_store
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


async def run_decision_cycle(symbols: list[str] | None = None, interval: str = "5minute"):
    """Run rule-based decision cycle for all symbols."""
    from trading_agent.ingest.watchlist_resolved import get_watchlist_symbols
    
    if symbols is None:
        symbols = get_watchlist_symbols()
    
    if not symbols:
        logger.warning("run_decision_cycle: no symbols in watchlist")
        return

    agent = RuleDecisionAgent()
    generator = CandidateGenerator()
    parquet = get_parquet_store()

    for symbol in symbols:
        try:
            # Load latest candles
            df = parquet.read_latest(symbol, interval, n=2000)
            if df is None or df.empty:
                logger.warning("no_data_for_symbol", symbol=symbol)
                continue

            candidates = generator.generate_for_symbol(df, symbol, interval)
            if not candidates:
                logger.debug("no_candidates_generated", symbol=symbol)
                continue

            for candidate in candidates:
                decision = agent.decide(candidate)
                logger.info("rule_decision", symbol=symbol, action=decision.action, confidence=decision.confidence)
                
                # Store decision to database
                await _store_rule_signal(symbol, decision, interval)
                
        except Exception as e:
            logger.error("rule_decision_failed", symbol=symbol, error=str(e))


async def _store_rule_signal(symbol: str, decision, interval: str):
    """Store rule signal to database."""
    try:
        from trading_agent.storage.db import Signal, get_database
        from datetime import datetime, UTC
        
        db = get_database()
        async with db.session() as session:
            signal = Signal(
                symbol=symbol,
                timestamp=datetime.now(UTC),
                interval=interval,
                action=decision.action,
                strategy="rule",
                confidence=decision.confidence,
                rsi=decision.rsi,
                adx=decision.adx,
                pattern_bias=decision.pattern_bias,
            )
            session.add(signal)
            await session.commit()
    except Exception as e:
        logger.error("store_rule_signal_failed", symbol=symbol, error=str(e))
