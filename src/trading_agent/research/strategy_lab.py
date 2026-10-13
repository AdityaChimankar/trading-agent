"""Strategy lab for rapid experimentation."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from trading_agent.core.logging_config import get_logger
from trading_agent.core.pattern_detection import compute_pattern_bias
from trading_agent.core.position_sizing import PositionSizingConfig, calculate_position_size
from trading_agent.core.quant_indicators import prepare_indicators
from trading_agent.core.signals import SignalParams, evaluate_signals
from trading_agent.storage.parquet_store import get_parquet_store

logger = get_logger(__name__)


@dataclass(frozen=True)
class LabConfig:
    """Strategy lab configuration."""

    forward_window: int = 12
    slippage_pct: float = 0.0005
    commission: float = 20.0


DEFAULT_LAB_CONFIG = LabConfig()


@dataclass
class StrategyResult:
    """Result of a strategy test."""

    params: SignalParams
    total_trades: int
    win_rate: float
    gross_ev: float
    net_ev: float
    total_return: float
    max_drawdown: float
    sharpe: float
    profit_factor: float


class StrategyLab:
    """Rapid strategy experimentation."""

    def __init__(self, config: LabConfig = DEFAULT_LAB_CONFIG):
        self.config = config

    def test_params(
        self,
        df: pd.DataFrame,
        params: SignalParams,
        position_config: PositionSizingConfig | None = None,
    ) -> StrategyResult:
        """Test a parameter set on historical data."""
        # Prepare data
        prepared = prepare_indicators(df)
        prepared["pattern_bias"] = compute_pattern_bias(
            prepared["opens"],
            prepared["highs"],
            prepared["lows"],
            prepared["closes"],
        )

        # Evaluate signals
        actions = evaluate_signals(
            prepared["rsis"],
            prepared["adxs"],
            prepared["pattern_bias"],
            params=params,
        )

        # Simulate trades
        trades = self._simulate_trades(prepared, actions, position_config)

        if not trades:
            return StrategyResult(
                params=params,
                total_trades=0,
                win_rate=0,
                gross_ev=0,
                net_ev=0,
                total_return=0,
                max_drawdown=0,
                sharpe=0,
                profit_factor=0,
            )

        # Calculate metrics
        pnls = [t["net_pnl"] for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        gross_returns = [t["gross_return"] for t in trades]
        net_returns = [t["net_return"] for t in trades]

        # Equity curve
        equity = 1.0
        equity_curve = [equity]
        for r in net_returns:
            equity *= 1 + r
            equity_curve.append(equity)

        equity_arr = np.array(equity_curve)
        peak = np.maximum.accumulate(equity_arr)
        drawdown = (equity_arr - peak) / peak
        max_dd = abs(np.min(drawdown)) * 100

        # Sharpe
        returns_arr = np.array(net_returns)
        sharpe = (
            np.mean(returns_arr) / np.std(returns_arr) * np.sqrt(252 * 78)
            if np.std(returns_arr) > 0
            else 0
        )

        return StrategyResult(
            params=params,
            total_trades=len(trades),
            win_rate=len(wins) / len(trades) * 100,
            gross_ev=np.mean(gross_returns) * 100,
            net_ev=np.mean(net_returns) * 100,
            total_return=(equity - 1) * 100,
            max_drawdown=max_dd,
            sharpe=sharpe,
            profit_factor=abs(sum(wins) / sum(losses)) if losses else float("inf"),
        )

    def _simulate_trades(
        self,
        prepared: dict,
        actions: np.ndarray,
        position_config: PositionSizingConfig | None,
    ) -> list[dict]:
        """Simulate trades from actions."""
        trades = []
        n = prepared["length"]
        fw = self.config.forward_window

        for i in range(n - fw):
            if actions[i] == 0:
                continue

            entry_price = prepared["closes"][i]
            exit_price = prepared["closes"][i + fw]
            action = "BUY" if actions[i] == 1 else "SELL"

            if action == "BUY":
                gross_return = (exit_price - entry_price) / entry_price
            else:
                gross_return = (entry_price - exit_price) / entry_price

            # Calculate position size
            if position_config:
                atr = prepared["atrs"][i]
                position = calculate_position_size(
                    entry_price=entry_price,
                    atr_value=atr,
                    portfolio_value=1000000,
                    config=position_config,
                )
                quantity = position.shares
            else:
                quantity = 100  # Default

            if quantity <= 0:
                continue

            gross_pnl = gross_return * entry_price * quantity
            commission = self.config.commission * 2
            slippage = entry_price * quantity * self.config.slippage_pct * 2
            net_pnl = gross_pnl - commission - slippage

            trades.append(
                {
                    "entry_idx": i,
                    "action": action,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "quantity": quantity,
                    "gross_return": gross_return,
                    "net_return": net_pnl / (entry_price * quantity)
                    if entry_price * quantity > 0
                    else 0,
                    "gross_pnl": gross_pnl,
                    "net_pnl": net_pnl,
                }
            )

        return trades

    def grid_search(
        self,
        df: pd.DataFrame,
        param_grid: dict[str, list],
    ) -> list[StrategyResult]:
        """Grid search over parameter combinations."""
        import itertools

        keys = list(param_grid.keys())
        results = []

        for values in itertools.product(*param_grid.values()):
            params = SignalParams(**dict(zip(keys, values, strict=False)))
            result = self.test_params(df, params)
            results.append(result)

        # Sort by net EV
        results.sort(key=lambda r: r.net_ev, reverse=True)
        return results

    def random_search(
        self,
        df: pd.DataFrame,
        param_ranges: dict[str, tuple],
        n_iter: int = 100,
    ) -> list[StrategyResult]:
        """Random search over parameter ranges."""
        results = []

        for _ in range(n_iter):
            params_dict = {}
            for key, (low, high) in param_ranges.items():
                if isinstance(low, int):
                    params_dict[key] = np.random.randint(low, high + 1)
                else:
                    params_dict[key] = np.random.uniform(low, high)

            params = SignalParams(**params_dict)
            result = self.test_params(df, params)
            results.append(result)

        results.sort(key=lambda r: r.net_ev, reverse=True)
        return results


def run_strategy_lab(
    symbol: str,
    interval: str = "5minute",
    param_grid: dict | None = None,
    **kwargs,
) -> list[StrategyResult]:
    """Convenience function to run strategy lab."""
    parquet = get_parquet_store()
    df = parquet.read_latest(symbol, interval, n=5000)

    if df.empty:
        raise ValueError(f"No data for {symbol}")

    lab = StrategyLab(**kwargs)

    if param_grid:
        return lab.grid_search(df, param_grid)
    else:
        # Default grid
        default_grid = {
            "rsi_oversold": [25, 30, 35],
            "rsi_overbought": [65, 70, 75],
            "adx_threshold": [15, 20, 25],
            "pattern_confirm_buy_rsi": [40, 45, 50],
            "pattern_confirm_sell_rsi": [50, 55, 60],
        }
        return lab.grid_search(df, default_grid)
