"""Shadow testing framework for strategy validation."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np

from trading_agent.core.logging_config import get_logger
from trading_agent.strategy.candidates import Candidate
from trading_agent.strategy.hybrid_strategy import HybridStrategy, HybridDecision

logger = get_logger(__name__)


@dataclass(frozen=True)
class ShadowTrade:
    """A shadow (paper) trade for testing."""

    symbol: str
    entry_time: datetime
    entry_price: float
    action: str  # BUY, SELL
    quantity: int
    stop_loss: float
    take_profit: float | None
    strategy: str
    confidence: float
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ShadowResult:
    """Result of a shadow trade."""

    trade: ShadowTrade
    exit_time: datetime | None
    exit_price: float | None
    pnl: float | None
    exit_reason: str | None  # "stop_loss", "take_profit", "time", "signal_reversal"
    duration_minutes: float | None


class ShadowTester:
    """Shadow testing framework for strategy validation."""

    def __init__(
        self,
        strategy: HybridStrategy,
        initial_capital: float = 1000000,
        max_positions: int = 10,
        position_size_pct: float = 0.02,
    ):
        self.strategy = strategy
        self.initial_capital = initial_capital
        self.max_positions = max_positions
        self.position_size_pct = position_size_pct

        self.open_trades: dict[str, ShadowTrade] = {}
        self.closed_trades: list[ShadowResult] = []
        self.equity_curve: list[tuple[datetime, float]] = [(datetime.now(UTC), initial_capital)]

        # Performance tracking
        self.daily_pnl: dict[str, float] = defaultdict(float)
        self.symbol_pnl: dict[str, float] = defaultdict(float)

    def process_candidate(
        self, candidate: Candidate, current_price: float | None = None
    ) -> HybridDecision | None:
        """Process a candidate through the strategy."""
        decision = self.strategy.decide(candidate)

        if decision.action == "HOLD":
            return None

        # Check if we already have a position in this symbol
        if candidate.symbol in self.open_trades:
            return None

        # Check max positions
        if len(self.open_trades) >= self.max_positions:
            return None

        # Create shadow trade
        price = current_price or candidate.close
        quantity = int(self.initial_capital * self.position_size_pct / price)

        if quantity <= 0:
            return None

        trade = ShadowTrade(
            symbol=candidate.symbol,
            entry_time=candidate.timestamp,
            entry_price=price,
            action=decision.action,
            quantity=quantity,
            stop_loss=decision.metadata.get("stop_loss", price * 0.98),
            take_profit=decision.metadata.get("take_profit"),
            strategy="hybrid",
            confidence=decision.confidence,
            metadata=decision.metadata,
        )

        self.open_trades[candidate.symbol] = trade
        logger.info("shadow_trade_opened", **trade.__dict__)

        return decision

    def update_positions(self, market_data: dict[str, dict]) -> list[ShadowResult]:
        """Update open positions with current market data."""
        closed = []

        for symbol, trade in list(self.open_trades.items()):
            if symbol not in market_data:
                continue

            data = market_data[symbol]
            current_price = data.get("close", trade.entry_price)
            high = data.get("high", current_price)
            low = data.get("low", current_price)
            timestamp = data.get("timestamp", datetime.now(UTC))

            exit_reason = None
            exit_price = None

            if trade.action == "BUY":
                # Check stop loss
                if low <= trade.stop_loss:
                    exit_reason = "stop_loss"
                    exit_price = trade.stop_loss
                # Check take profit
                elif trade.take_profit and high >= trade.take_profit:
                    exit_reason = "take_profit"
                    exit_price = trade.take_profit
            # Check stop loss
            elif high >= trade.stop_loss:
                exit_reason = "stop_loss"
                exit_price = trade.stop_loss
            # Check take profit
            elif trade.take_profit and low <= trade.take_profit:
                exit_reason = "take_profit"
                exit_price = trade.take_profit

            # Time-based exit (end of day)
            if exit_reason is None:
                # Simplified: exit at 3:20 PM
                if timestamp.hour == 15 and timestamp.minute >= 20:
                    exit_reason = "time"
                    exit_price = current_price

            if exit_reason:
                # Calculate PnL
                if trade.action == "BUY":
                    pnl = (exit_price - trade.entry_price) * trade.quantity
                else:
                    pnl = (trade.entry_price - exit_price) * trade.quantity

                # Subtract costs (brokerage + STT + slippage)
                costs = (
                    trade.entry_price * trade.quantity * 0.0005
                    + exit_price * trade.quantity * 0.0005
                )
                pnl -= costs

                result = ShadowResult(
                    trade=trade,
                    exit_time=timestamp,
                    exit_price=exit_price,
                    pnl=pnl,
                    exit_reason=exit_reason,
                    duration_minutes=(timestamp - trade.entry_time).total_seconds() / 60,
                )

                closed.append(result)
                self.closed_trades.append(result)
                del self.open_trades[symbol]

                # Update tracking
                date_key = timestamp.date().isoformat()
                self.daily_pnl[date_key] += pnl
                self.symbol_pnl[symbol] += pnl

                # Update equity curve
                current_equity = self.equity_curve[-1][1] + pnl
                self.equity_curve.append((timestamp, current_equity))

                logger.info("shadow_trade_closed", symbol=symbol, pnl=pnl, reason=exit_reason)

        return closed

    def get_performance_summary(self) -> dict:
        """Get performance summary."""
        if not self.closed_trades:
            return {"total_trades": 0}

        pnls = [t.pnl for t in self.closed_trades if t.pnl is not None]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        equity_values = [e[1] for e in self.equity_curve]
        peak = np.maximum.accumulate(equity_values)
        drawdown = (np.array(equity_values) - peak) / peak
        max_drawdown = abs(drawdown.min()) if len(drawdown) > 0 else 0

        return {
            "total_trades": len(self.closed_trades),
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate": len(wins) / len(pnls) * 100 if pnls else 0,
            "total_pnl": sum(pnls),
            "avg_win": np.mean(wins) if wins else 0,
            "avg_loss": np.mean(losses) if losses else 0,
            "profit_factor": abs(sum(wins) / sum(losses)) if losses else float("inf"),
            "max_drawdown_pct": max_drawdown * 100,
            "sharpe_ratio": self._calculate_sharpe(pnls),
            "daily_pnl": dict(self.daily_pnl),
            "symbol_pnl": dict(self.symbol_pnl),
        }

    def _calculate_sharpe(self, returns: list[float], risk_free: float = 0.0) -> float:
        """Calculate Sharpe ratio."""
        if len(returns) < 2:
            return 0.0
        excess = np.array(returns) - risk_free / 252
        return float(np.mean(excess) / np.std(excess) * np.sqrt(252)) if np.std(excess) > 0 else 0.0

    def get_open_positions(self) -> list[ShadowTrade]:
        """Get currently open shadow positions."""
        return list(self.open_trades.values())

    def reset(self) -> None:
        """Reset shadow tester."""
        self.open_trades.clear()
        self.closed_trades.clear()
        self.equity_curve = [(datetime.now(UTC), self.initial_capital)]
        self.daily_pnl.clear()
        self.symbol_pnl.clear()


async def run_shadow_backtest(
    strategy: HybridStrategy,
    symbols: list[str],
    start_date: datetime,
    end_date: datetime,
    interval: str = "5minute",
) -> dict:
    """Run shadow backtest on historical data."""
    from trading_agent.storage.parquet_store import get_parquet_store

    parquet = get_parquet_store()
    tester = ShadowTester(strategy)

    # Load data for all symbols
    all_data = {}
    for symbol in symbols:
        df = parquet.read_candles(symbol, interval, start=start_date, end=end_date)
        if not df.empty:
            all_data[symbol] = df

    # Process chronologically
    all_timestamps = set()
    for df in all_data.values():
        all_timestamps.update(df["timestamp"].tolist())

    for timestamp in sorted(all_timestamps):
        market_data = {}
        for symbol, df in all_data.items():
            row = df[df["timestamp"] == timestamp]
            if not row.empty:
                market_data[symbol] = row.iloc[0].to_dict()

        # Update positions
        tester.update_positions(market_data)

        # Process new candidates
        for symbol, df in all_data.items():
            row = df[df["timestamp"] == timestamp]
            if row.empty:
                continue

            # Create candidate (simplified)
            from trading_agent.strategy.candidates import CandidateGenerator

            generator = CandidateGenerator()
            candidate = generator.generate_for_symbol(symbol, interval, lookback=100)

            if candidate:
                tester.process_candidate(candidate, market_data.get(symbol, {}).get("close"))

    return tester.get_performance_summary()


async def run_shadow_cycle(symbols: list[str] | None = None, interval: str = "5minute"):
    """Run shadow testing cycle for all symbols."""
    from trading_agent.ingest.watchlist_resolved import get_watchlist_symbols
    from trading_agent.storage.parquet_store import get_parquet_store
    from trading_agent.strategy.candidates import CandidateGenerator
    from trading_agent.strategy.hybrid_strategy import HybridStrategy
    
    if symbols is None:
        symbols = get_watchlist_symbols()
    
    if not symbols:
        logger.warning("run_shadow_cycle: no symbols in watchlist")
        return

    hybrid = HybridStrategy()
    tester = ShadowTester(hybrid)
    generator = CandidateGenerator()
    parquet = get_parquet_store()

    for symbol in symbols:
        try:
            # Load latest candles
            df = parquet.read_latest(symbol, interval, n=500)
            if df is None or df.empty:
                logger.warning("no_data_for_symbol", symbol=symbol)
                continue

            candidates = generator.generate_for_symbol(df, symbol, interval)
            if not candidates:
                logger.debug("no_candidates_generated", symbol=symbol)
                continue

            for candidate in candidates:
                decision = tester.process_candidate(candidate)
                if decision:
                    logger.info("shadow_test", symbol=symbol, action=decision.action, confidence=decision.confidence)
                    
                    # Store shadow signal to database
                    await _store_shadow_signal(symbol, decision, interval)
                
        except Exception as e:
            logger.error("shadow_test_failed", symbol=symbol, error=str(e))


async def _store_shadow_signal(symbol: str, decision, interval: str):
    """Store shadow signal to database."""
    try:
        from trading_agent.storage.db import StrategySignal, get_database
        from datetime import datetime, UTC
        
        db = get_database()
        async with db.session() as session:
            signal = StrategySignal(
                symbol=symbol,
                timestamp=datetime.now(UTC),
                interval=interval,
                strategy="hybrid",
                vote=decision.action,
                confidence=decision.confidence,
            )
            session.add(signal)
            await session.commit()
    except Exception as e:
        logger.error("store_shadow_signal_failed", symbol=symbol, error=str(e))
