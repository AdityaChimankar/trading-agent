"""Strategy candidates and signal generation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from trading_agent.core.logging_config import get_logger
from trading_agent.core.position_sizing import PositionSizingConfig, calculate_position_size
from trading_agent.core.signals import SignalParams, evaluate_signals, prepare_symbol_data
from trading_agent.storage.parquet_store import get_parquet_store

logger = get_logger(__name__)


@dataclass(frozen=True)
class Candidate:
    """Trading candidate with all signal info."""

    symbol: str
    timestamp: datetime
    interval: str
    close: float
    rsi: float
    adx: float
    atr: float
    pattern_bias: int  # 0=neutral, 1=bullish, 2=bearish
    rule_action: str  # BUY, SELL, HOLD
    rule_confidence: float
    ml_action: str | None = None
    ml_confidence: float = 0.0
    llm_action: str | None = None
    llm_confidence: float = 0.0
    sentiment: float = 0.0
    volume: int = 0
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CandidateConfig:
    """Configuration for candidate generation."""

    rsi_period: int = 14
    adx_period: int = 14
    atr_period: int = 14
    swing_order: int = 5
    signal_params: SignalParams = field(default_factory=SignalParams)
    position_config: PositionSizingConfig = field(default_factory=PositionSizingConfig)
    min_adx: float = 15.0
    min_volume: int = 10000


DEFAULT_CONFIG = CandidateConfig()


class CandidateGenerator:
    """Generate trading candidates from market data."""

    def __init__(
        self,
        config: CandidateConfig = DEFAULT_CONFIG,
        parquet_store=None,
    ):
        self.config = config
        self.parquet = parquet_store or get_parquet_store()

    def generate_for_symbol(
        self,
        symbol: str,
        interval: str = "5minute",
        lookback: int = 100,
        portfolio_value: float = 1000000,
    ) -> Candidate | None:
        """Generate candidate for a single symbol."""
        # Read latest data
        df = self.parquet.read_latest(symbol, interval, n=lookback + 50)
        if (
            df.empty
            or len(df)
            < max(self.config.rsi_period, self.config.adx_period, self.config.atr_period) + 10
        ):
            return None

        # Prepare indicators
        prepared = prepare_symbol_data(
            df,
            rsi_period=self.config.rsi_period,
            adx_period=self.config.adx_period,
            atr_period=self.config.atr_period,
            swing_order=self.config.swing_order,
        )

        # Get latest values
        idx = -1
        latest = {
            "symbol": symbol,
            "timestamp": prepared["timestamps"][idx],
            "interval": interval,
            "close": prepared["closes"][idx],
            "rsi": prepared["rsis"][idx],
            "adx": prepared["adxs"][idx],
            "atr": prepared["atrs"][idx],
            "pattern_bias": int(prepared["pattern_bias"][idx]),
            "volume": int(prepared["volumes"][idx]),
        }

        # Check data quality
        if any(np.isnan([latest["rsi"], latest["adx"], latest["atr"]])):
            return None

        if latest["adx"] < self.config.min_adx:
            return None

        if latest["volume"] < self.config.min_volume:
            return None

        # Evaluate rule-based signal
        actions = evaluate_signals(
            prepared["rsis"][-1:],
            prepared["adxs"][-1:],
            prepared["pattern_bias"][-1:],
            params=self.config.signal_params,
        )
        rule_action = ["HOLD", "BUY", "SELL"][actions[0]]
        rule_confidence = self._calculate_rule_confidence(latest, rule_action)

        # Position sizing
        position = calculate_position_size(
            entry_price=latest["close"],
            atr_value=latest["atr"],
            portfolio_value=portfolio_value,
            config=self.config.position_config,
            atr_percentile=0.5,  # Would compute from history
        )

        return Candidate(
            symbol=symbol,
            timestamp=latest["timestamp"],
            interval=interval,
            close=latest["close"],
            rsi=latest["rsi"],
            adx=latest["adx"],
            atr=latest["atr"],
            pattern_bias=latest["pattern_bias"],
            rule_action=rule_action,
            rule_confidence=rule_confidence,
            volume=latest["volume"],
            metadata={
                "position_size": position.shares,
                "stop_loss": position.stop_price,
                "take_profit": position.take_profit_price,
                "risk_amount": position.risk_amount,
            },
        )

    def _calculate_rule_confidence(self, data: dict, action: str) -> float:
        """Calculate confidence for rule-based signal."""
        if action == "HOLD":
            return 0.0

        confidence = 0.5  # Base

        # ADX strength
        if data["adx"] > 25:
            confidence += 0.15
        elif data["adx"] > 20:
            confidence += 0.1

        # RSI alignment
        if action == "BUY" and data["rsi"] < 30:
            confidence += 0.15
        elif action == "SELL" and data["rsi"] > 70:
            confidence += 0.15
        elif action == "BUY" and data["rsi"] < 45:
            confidence += 0.1
        elif action == "SELL" and data["rsi"] > 55:
            confidence += 0.1

        # Pattern confirmation
        if action == "BUY" and data["pattern_bias"] == 1:
            confidence += 0.1
        elif action == "SELL" and data["pattern_bias"] == 2:
            confidence += 0.1

        return min(1.0, confidence)

    def generate_all(
        self,
        symbols: list[str],
        interval: str = "5minute",
        portfolio_value: float = 1000000,
    ) -> list[Candidate]:
        """Generate candidates for multiple symbols."""
        candidates = []
        for symbol in symbols:
            try:
                candidate = self.generate_for_symbol(
                    symbol, interval, portfolio_value=portfolio_value
                )
                if candidate and candidate.rule_action != "HOLD":
                    candidates.append(candidate)
            except Exception as e:
                logger.error("candidate_generation_failed", symbol=symbol, error=str(e))

        # Sort by confidence descending
        candidates.sort(key=lambda c: c.rule_confidence, reverse=True)
        return candidates


def generate_candidates(
    symbols: list[str],
    interval: str = "5minute",
    portfolio_value: float = 1000000,
    **kwargs,
) -> list[Candidate]:
    """Convenience function to generate candidates."""
    generator = CandidateGenerator(**kwargs)
    return generator.generate_all(symbols, interval, portfolio_value)
