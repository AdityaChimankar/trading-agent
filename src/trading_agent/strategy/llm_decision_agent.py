"""LLM-based decision agent."""

from __future__ import annotations

import json
from dataclasses import dataclass

from trading_agent.core.llm_client import (
    LLMClient,
    LLMRequest,
    LLMResponse,
    MockLLMClient,
    create_llm_client,
)
from trading_agent.core.logging_config import get_logger
from trading_agent.strategy.candidates import Candidate

logger = get_logger(__name__)


@dataclass(frozen=True)
class LLMDecision:
    """LLM decision output."""

    action: str  # BUY, SELL, HOLD
    confidence: float
    reasoning: str
    key_factors: list[str]
    risk_assessment: str
    # No decision was made (provider error or unreadable reply): never stored as a HOLD.
    failed: bool = False

# A provider that fails this many symbols in a row is down or misconfigured (on
# 2026-10-26 a retired OpenRouter model returned 404 for every call), so the cycle stops.
MAX_CONSECUTIVE_FAILURES = 3


SYSTEM_PROMPT = """You are an expert intraday trader for the Indian NSE market.
Analyze the given technical data and provide a trading decision.

Return ONLY a JSON object with these exact fields:
{
  "action": "BUY" | "SELL" | "HOLD",
  "confidence": 0.0-1.0,
  "reasoning": "Brief explanation of your decision",
  "key_factors": ["factor1", "factor2", ...],
  "risk_assessment": "LOW" | "MEDIUM" | "HIGH"
}

Consider:
- RSI: Oversold (<30) = bullish, Overbought (>70) = bearish
- ADX: >25 = strong trend, >20 = trend present
- Pattern bias: 1=bullish pattern, 2=bearish pattern, 0=none
- ATR: Volatility measure for position sizing
- Volume: Confirmation of price action
- Recent price action vs key levels

Be conservative. Default to HOLD unless there's a clear setup.
"""


USER_PROMPT_TEMPLATE = """Analyze this NSE intraday setup:

Symbol: {symbol}
Interval: {interval}
Current Price: {close:.2f}

Technical Indicators:
- RSI(14): {rsi:.1f}
- ADX(14): {adx:.1f}
- ATR(14): {atr:.2f}
- Pattern Bias: {pattern_bias} (0=neutral, 1=bullish, 2=bearish)
- Volume: {volume:,}
- Rule Signal: {rule_action} (confidence: {rule_confidence:.2f})

Provide your trading decision as JSON."""


class LLMDecisionAgent:
    """LLM-based trading decision agent."""

    def __init__(
        self,
        client: LLMClient | None = None,
        system_prompt: str = SYSTEM_PROMPT,
        user_template: str = USER_PROMPT_TEMPLATE,
        use_mock: bool = False,
    ):
        if use_mock:
            self.client = MockLLMClient()
        elif client is None:
            self.client = create_llm_client(tier="deep")
        else:
            self.client = client
        self.system_prompt = system_prompt
        self.user_template = user_template

    def _build_prompt(self, candidate: Candidate) -> str:
        """Build user prompt for candidate."""
        pattern_names = {0: "Neutral", 1: "Bullish", 2: "Bearish"}
        return self.user_template.format(
            symbol=candidate.symbol,
            interval=candidate.interval,
            close=candidate.close,
            rsi=candidate.rsi,
            adx=candidate.adx,
            atr=candidate.atr,
            pattern_bias=pattern_names.get(candidate.pattern_bias, "Unknown"),
            volume=candidate.volume,
            rule_action=candidate.rule_action,
            rule_confidence=candidate.rule_confidence,
        )

    def _parse_response(self, response: LLMResponse) -> LLMDecision:
        """Parse LLM response into decision."""
        try:
            data = json.loads(response.content)
            return LLMDecision(
                action=data.get("action", "HOLD").upper(),
                confidence=float(data.get("confidence", 0.0)),
                reasoning=data.get("reasoning", "No reasoning provided"),
                key_factors=data.get("key_factors", []),
                risk_assessment=data.get("risk_assessment", "MEDIUM"),
            )
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(
                "llm_response_parse_failed", error=str(e), content=response.content[:200]
            )
            return LLMDecision(
                action="HOLD",
                confidence=0.0,
                reasoning=f"Failed to parse LLM response: {e}",
                key_factors=[],
                risk_assessment="MEDIUM",
                failed=True,
            )

    async def decide(self, candidate: Candidate) -> LLMDecision:
        """Make LLM-based decision for a candidate."""
        prompt = self._build_prompt(candidate)
        request = LLMRequest(
            system_prompt=self.system_prompt,
            user_prompt=prompt,
            temperature=0.1,
            max_tokens=500,
            response_format={"type": "json_object"},
        )

        try:
            response = await self.client.complete(request)
            return self._parse_response(response)
        except Exception as e:
            logger.error("llm_decision_failed", symbol=candidate.symbol, error=str(e))
            return LLMDecision(
                action="HOLD",
                confidence=0.0,
                reasoning=f"LLM error: {e}",
                key_factors=[],
                risk_assessment="HIGH",
                failed=True,
            )

    async def decide_batch(self, candidates: list[Candidate]) -> list[LLMDecision]:
        """Make decisions for multiple candidates."""
        # Could batch for efficiency, but sequential for now
        decisions = []
        for candidate in candidates:
            decisions.append(await self.decide(candidate))
        return decisions

    async def close(self) -> None:
        """Close LLM client."""
        await self.client.close()


async def run_llm_decision_cycle(symbols: list[str] | None = None, interval: str = "5minute"):
    """Run LLM decision cycle for all symbols."""
    from trading_agent.ingest.watchlist_resolved import get_watchlist_symbols
    from trading_agent.storage.parquet_store import get_parquet_store
    from trading_agent.strategy.candidates import CandidateGenerator
    
    if symbols is None:
        symbols = get_watchlist_symbols()
    
    if not symbols:
        logger.warning("run_llm_decision_cycle: no symbols in watchlist")
        return

    # Check LLM kill switch
    import json
    from pathlib import Path
    kill_switch = Path("data/llm_kill_switch.json")
    if kill_switch.exists():
        try:
            with open(kill_switch) as f:
                data = json.load(f)
            if data.get("killed", False):
                logger.info("LLM decision cycle skipped: kill switch active")
                return
        except Exception:
            pass

    agent = LLMDecisionAgent(use_mock=False)
    generator = CandidateGenerator()
    parquet = get_parquet_store()
    failures = 0

    for symbol in symbols:
        try:
            # Load latest candles
            df = parquet.read_latest(symbol, interval, n=500)
            if df is None or df.empty:
                logger.warning("no_data_for_symbol", symbol=symbol)
                continue

            candidate = generator.generate_from_data(df, symbol, interval)
            if candidate is None:
                logger.debug("no_candidates_generated", symbol=symbol)
                continue

            decision = await agent.decide(candidate)
            if decision.failed:
                failures += 1
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    logger.error("llm_decision_cycle_aborted", consecutive_failures=failures,
                                 last_error=decision.reasoning[:200])
                    break
                continue
            failures = 0
            logger.info(
                "llm_decision",
                symbol=symbol,
                action=decision.action,
                confidence=decision.confidence,
            )

            await _store_llm_signal(symbol, decision, interval)
                
        except Exception as e:
            logger.error("llm_decision_failed", symbol=symbol, error=str(e))

    await agent.close()


async def _store_llm_signal(symbol: str, decision, interval: str):
    """Store LLM signal to database."""
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
                strategy="llm",
                confidence=decision.confidence,
                rsi=0.0,
                adx=0.0,
                pattern_bias=0,
            )
            session.add(signal)
            await session.commit()
    except Exception as e:
        logger.error("store_llm_signal_failed", symbol=symbol, error=str(e))
