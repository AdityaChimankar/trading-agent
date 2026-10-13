"""Backtesting framework."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from trading_agent.core.logging_config import get_logger
from trading_agent.storage.parquet_store import get_parquet_store
from trading_agent.strategy.candidates import Candidate, CandidateGenerator

logger = get_logger(__name__)


@dataclass(frozen=True)
class BacktestConfig:
    """Backtest configuration."""

    initial_capital: float = 1000000
    position_size_pct: float = 0.02
    max_positions: int = 10
    commission_per_trade: float = 20.0
    slippage_pct: float = 0.0005
    stop_loss_atr_mult: float = 1.5
    take_profit_atr_mult: float = 2.0
    use_trailing_stop: bool = False
    trailing_atr_mult: float = 1.0


DEFAULT_BACKTEST_CONFIG = BacktestConfig()


@dataclass
class BacktestTrade:
    """A single backtest trade."""

    symbol: str
    entry_time: datetime
    entry_price: float
    exit_time: datetime | None
    exit_price: float | None
    quantity: int
    action: str  # BUY, SELL
    gross_pnl: float = 0.0
    commission: float = 0.0
    slippage: float = 0.0
    net_pnl: float = 0.0
    exit_reason: str | None = None


@dataclass
class BacktestResult:
    """Backtest results."""

    config: BacktestConfig
    start_date: datetime
    end_date: datetime
    symbols: list[str]
    trades: list[BacktestTrade]
    equity_curve: list[tuple[datetime, float]]

    # Metrics
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    total_pnl_pct: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    avg_holding_minutes: float = 0.0


class BacktestEngine:
    """Vectorized backtesting engine."""

    def __init__(
        self,
        config: BacktestConfig = DEFAULT_BACKTEST_CONFIG,
        candidate_generator: CandidateGenerator | None = None,
    ):
        self.config = config
        self.generator = candidate_generator or CandidateGenerator()
        self.parquet = get_parquet_store()

    def run(
        self,
        symbols: list[str],
        start_date: datetime,
        end_date: datetime,
        interval: str = "5minute",
    ) -> BacktestResult:
        """Run backtest on historical data."""
        # Load data for all symbols
        data = {}
        for symbol in symbols:
            df = self.parquet.read_candles(symbol, interval, start=start_date, end=end_date)
            if not df.empty:
                data[symbol] = df

        if not data:
            raise ValueError("No data available for backtest")

        # Generate all candidates across time
        all_trades = []
        equity = self.config.initial_capital
        equity_curve = [(start_date, equity)]
        open_positions: dict[str, BacktestTrade] = {}

        # Get all timestamps
        all_timestamps = set()
        for df in data.values():
            all_timestamps.update(df["timestamp"].tolist())

        for timestamp in sorted(all_timestamps):
            # Update open positions
            for symbol, trade in list(open_positions.items()):
                if symbol not in data:
                    continue

                df = data[symbol]
                row = df[df["timestamp"] == timestamp]
                if row.empty:
                    continue

                current_price = row.iloc[0]["close"]
                high = row.iloc[0]["high"]
                low = row.iloc[0]["low"]

                # Check exits
                exit_price, exit_reason = self._check_exit(trade, current_price, high, low)
                if exit_price:
                    self._close_trade(trade, exit_price, timestamp, exit_reason)
                    all_trades.append(trade)
                    del open_positions[symbol]
                    equity += trade.net_pnl
                    equity_curve.append((timestamp, equity))

            # Generate new signals
            for symbol, df in data.items():
                row = df[df["timestamp"] == timestamp]
                if row.empty:
                    continue

                if symbol in open_positions:
                    continue

                if len(open_positions) >= self.config.max_positions:
                    continue

                # Create candidate from historical data up to this point
                hist_df = df[df["timestamp"] <= timestamp].tail(200)
                if len(hist_df) < 50:
                    continue

                candidate = self.generator.generate_for_symbol(
                    symbol, interval, lookback=len(hist_df)
                )
                if not candidate or candidate.rule_action == "HOLD":
                    continue

                # Execute trade
                trade = self._execute_trade(candidate, timestamp, row.iloc[0])
                if trade:
                    open_positions[symbol] = trade

        # Close remaining positions at end
        for symbol, trade in open_positions.items():
            if symbol in data:
                last_price = data[symbol].iloc[-1]["close"]
                self._close_trade(trade, last_price, end_date, "end_of_period")
                all_trades.append(trade)
                equity += trade.net_pnl

        equity_curve.append((end_date, equity))

        return self._calculate_results(
            config=self.config,
            start_date=start_date,
            end_date=end_date,
            symbols=symbols,
            trades=all_trades,
            equity_curve=equity_curve,
        )

    def _execute_trade(
        self,
        candidate: Candidate,
        timestamp: datetime,
        candle: pd.Series,
    ) -> BacktestTrade | None:
        """Execute a trade from candidate."""
        entry_price = candle["close"]
        quantity = int(self.config.initial_capital * self.config.position_size_pct / entry_price)

        if quantity <= 0:
            return None

        # Calculate stop loss and take profit
        stop_distance = candidate.atr * self.config.stop_loss_atr_mult
        if candidate.rule_action == "BUY":
            entry_price - stop_distance
            entry_price + stop_distance * (
                self.config.take_profit_atr_mult / self.config.stop_loss_atr_mult
            )
        else:
            entry_price + stop_distance
            entry_price - stop_distance * (
                self.config.take_profit_atr_mult / self.config.stop_loss_atr_mult
            )

        commission = self.config.commission_per_trade
        slippage = entry_price * quantity * self.config.slippage_pct

        return BacktestTrade(
            symbol=candidate.symbol,
            entry_time=timestamp,
            entry_price=entry_price,
            exit_time=None,
            exit_price=None,
            quantity=quantity,
            action=candidate.rule_action,
            commission=commission,
            slippage=slippage,
        )

    def _check_exit(
        self,
        trade: BacktestTrade,
        current_price: float,
        high: float,
        low: float,
    ) -> tuple[float | None, str | None]:
        """Check if trade should be exited."""
        if trade.action == "BUY":
            # Check stop loss
            if low <= trade.stop_loss:
                return trade.stop_loss, "stop_loss"
            # Check take profit
            if trade.take_profit and high >= trade.take_profit:
                return trade.take_profit, "take_profit"
        else:
            # Check stop loss
            if high >= trade.stop_loss:
                return trade.stop_loss, "stop_loss"
            # Check take profit
            if trade.take_profit and low <= trade.take_profit:
                return trade.take_profit, "take_profit"

        return None, None

    def _close_trade(
        self,
        trade: BacktestTrade,
        exit_price: float,
        exit_time: datetime,
        exit_reason: str,
    ) -> None:
        """Close a trade and calculate PnL."""
        trade.exit_time = exit_time
        trade.exit_price = exit_price
        trade.exit_reason = exit_reason

        if trade.action == "BUY":
            gross_pnl = (exit_price - trade.entry_price) * trade.quantity
        else:
            gross_pnl = (trade.entry_price - exit_price) * trade.quantity

        trade.gross_pnl = gross_pnl
        trade.net_pnl = gross_pnl - trade.commission - trade.slippage

    def _calculate_results(
        self,
        config: BacktestConfig,
        start_date: datetime,
        end_date: datetime,
        symbols: list[str],
        trades: list[BacktestTrade],
        equity_curve: list[tuple[datetime, float]],
    ) -> BacktestResult:
        """Calculate backtest metrics."""
        if not trades:
            return BacktestResult(
                config=config,
                start_date=start_date,
                end_date=end_date,
                symbols=symbols,
                trades=[],
                equity_curve=equity_curve,
            )

        pnls = [t.net_pnl for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        # Equity curve drawdown
        equity_values = [e[1] for e in equity_curve]
        peak = np.maximum.accumulate(equity_values)
        drawdown = (np.array(equity_values) - peak) / peak
        max_drawdown = abs(np.min(drawdown)) * 100 if len(drawdown) > 0 else 0

        # Sharpe ratio
        returns = np.diff(equity_values) / equity_values[:-1]
        sharpe = (
            np.mean(returns) / np.std(returns) * np.sqrt(252 * 78) if np.std(returns) > 0 else 0
        )  # 78 5-min periods per day

        return BacktestResult(
            config=config,
            start_date=start_date,
            end_date=end_date,
            symbols=symbols,
            trades=trades,
            equity_curve=equity_curve,
            total_trades=len(trades),
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate=len(wins) / len(trades) * 100 if trades else 0,
            total_pnl=sum(pnls),
            total_pnl_pct=sum(pnls) / config.initial_capital * 100,
            avg_win=np.mean(wins) if wins else 0,
            avg_loss=np.mean(losses) if losses else 0,
            profit_factor=abs(sum(wins) / sum(losses)) if losses else float("inf"),
            max_drawdown=abs(np.min(np.array(equity_values) - peak)),
            max_drawdown_pct=max_drawdown,
            sharpe_ratio=sharpe,
            avg_holding_minutes=np.mean(
                [(t.exit_time - t.entry_time).total_seconds() / 60 for t in trades if t.exit_time]
            )
            if trades
            else 0,
        )


def run_backtest(
    symbols: list[str],
    start_date: datetime,
    end_date: datetime,
    interval: str = "5minute",
    **kwargs,
) -> BacktestResult:
    """Convenience function to run backtest."""
    engine = BacktestEngine(**kwargs)
    return engine.run(symbols, start_date, end_date, interval)
