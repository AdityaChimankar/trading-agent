"""Research module - Backtesting, edge diagnosis, strategy lab."""

from trading_agent.research.backtest import (
    DEFAULT_BACKTEST_CONFIG,
    BacktestConfig,
    BacktestEngine,
    BacktestResult,
    BacktestTrade,
    run_backtest,
)
from trading_agent.research.diagnose_edge import (
    DEFAULT_EDGE_CONFIG,
    EdgeConfig,
    EdgeDiagnoser,
    EdgeResult,
    diagnose_edge,
)
from trading_agent.research.strategy_lab import (
    DEFAULT_LAB_CONFIG,
    LabConfig,
    StrategyLab,
    StrategyResult,
    run_strategy_lab,
)
from trading_agent.research.walk_forward_optimizer import (
    run_walk_forward,
    run_walk_forward_multi,
)

__all__ = [
    # Backtest
    "BacktestConfig",
    "BacktestTrade",
    "BacktestResult",
    "BacktestEngine",
    "run_backtest",
    "DEFAULT_BACKTEST_CONFIG",
    # Edge Diagnosis
    "EdgeConfig",
    "EdgeResult",
    "EdgeDiagnoser",
    "diagnose_edge",
    "DEFAULT_EDGE_CONFIG",
    # Strategy Lab
    "LabConfig",
    "StrategyResult",
    "StrategyLab",
    "run_strategy_lab",
    "DEFAULT_LAB_CONFIG",
    # Walk-Forward Optimizer
    "run_walk_forward",
    "run_walk_forward_multi",
]
