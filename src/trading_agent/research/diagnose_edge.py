"""Edge diagnosis for rule-based signals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd
from scipy import stats

from trading_agent.core.logging_config import get_logger
from trading_agent.core.pattern_detection import compute_pattern_bias
from trading_agent.core.quant_indicators import prepare_indicators
from trading_agent.core.signals import SignalParams, evaluate_signals
from trading_agent.storage.parquet_store import get_parquet_store

logger = get_logger(__name__)


@dataclass(frozen=True)
class EdgeConfig:
    """Edge diagnosis configuration."""

    forward_window: int = 12  # 12 * 5min = 60min
    slippage_pct: float = 0.0005
    min_trades: int = 30
    random_baseline_samples: int = 10000


DEFAULT_EDGE_CONFIG = EdgeConfig()


@dataclass(frozen=True)
class EdgeResult:
    """Edge analysis result."""

    symbol: str
    total_signals: int
    buy_signals: int
    sell_signals: int

    # Gross returns
    buy_gross_ev: float
    sell_gross_ev: float
    all_gross_ev: float

    # Net returns (after slippage)
    buy_net_ev: float
    sell_net_ev: float
    all_net_ev: float

    # vs Random
    buy_vs_random: float
    sell_vs_random: float
    all_vs_random: float

    # T-statistics
    buy_t_stat: float
    sell_t_stat: float
    all_t_stat: float

    # Win rates
    buy_win_rate: float
    sell_win_rate: float

    # Pattern bias
    pattern_bullish_pct: float
    pattern_bearish_pct: float
    pattern_neutral_pct: float


class EdgeDiagnoser:
    """Diagnose edge of trading signals."""

    def __init__(self, config: EdgeConfig = DEFAULT_EDGE_CONFIG):
        self.config = config

    def analyze_symbol(
        self,
        symbol: str,
        interval: str = "5minute",
        params: SignalParams | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> EdgeResult:
        """Analyze edge for a single symbol."""
        parquet = get_parquet_store()

        df = parquet.read_candles(symbol, interval, start=start, end=end)
        if df.empty or len(df) < 100:
            raise ValueError(f"Insufficient data for {symbol}")

        # Prepare indicators
        prepared = prepare_indicators(df)
        prepared["pattern_bias"] = compute_pattern_bias(
            prepared["opens"],
            prepared["highs"],
            prepared["lows"],
            prepared["closes"],
        )
        prepared["symbol"] = symbol
        prepared["timestamps"] = df["timestamp"].to_numpy()
        prepared["length"] = len(df)

        # Evaluate signals
        signal_params = params or SignalParams()
        actions = evaluate_signals(
            prepared["rsis"],
            prepared["adxs"],
            prepared["pattern_bias"],
            params=signal_params,
        )

        # Get signal indices
        buy_idx = np.where(actions == 1)[0]
        sell_idx = np.where(actions == 2)[0]

        # Filter valid indices (need forward window)
        max_idx = len(df) - self.config.forward_window
        buy_idx = buy_idx[buy_idx < max_idx]
        sell_idx = sell_idx[sell_idx < max_idx]

        # Calculate returns
        buy_returns = self._calculate_returns(df, buy_idx, "BUY")
        sell_returns = self._calculate_returns(df, sell_idx, "SELL")

        # Random baseline
        random_returns = self._random_baseline(df)

        # Calculate metrics
        buy_gross_ev = np.mean(buy_returns) if len(buy_returns) > 0 else 0
        sell_gross_ev = np.mean(sell_returns) if len(sell_returns) > 0 else 0
        all_gross_ev = (
            np.mean(np.concatenate([buy_returns, sell_returns]))
            if (len(buy_returns) + len(sell_returns)) > 0
            else 0
        )

        random_ev = np.mean(random_returns)

        # Net returns (after slippage)
        buy_net_ev = buy_gross_ev - 2 * self.config.slippage_pct if len(buy_returns) > 0 else 0
        sell_net_ev = sell_gross_ev - 2 * self.config.slippage_pct if len(sell_returns) > 0 else 0
        all_net_ev = (
            all_gross_ev - 2 * self.config.slippage_pct
            if (len(buy_returns) + len(sell_returns)) > 0
            else 0
        )

        # T-statistics
        buy_t = stats.ttest_1samp(buy_returns, random_ev).statistic if len(buy_returns) > 1 else 0
        sell_t = (
            stats.ttest_1samp(sell_returns, random_ev).statistic if len(sell_returns) > 1 else 0
        )
        all_t = (
            stats.ttest_1samp(np.concatenate([buy_returns, sell_returns]), random_ev).statistic
            if (len(buy_returns) + len(sell_returns)) > 1
            else 0
        )

        # Win rates
        buy_wr = np.mean(np.array(buy_returns) > 0) * 100 if len(buy_returns) > 0 else 0
        sell_wr = np.mean(np.array(sell_returns) > 0) * 100 if len(sell_returns) > 0 else 0

        # Pattern bias
        pattern_codes = prepared["pattern_bias"]
        bullish_pct = np.mean(pattern_codes == 1) * 100
        bearish_pct = np.mean(pattern_codes == 2) * 100
        neutral_pct = np.mean(pattern_codes == 0) * 100

        return EdgeResult(
            symbol=symbol,
            total_signals=len(buy_idx) + len(sell_idx),
            buy_signals=len(buy_idx),
            sell_signals=len(sell_idx),
            buy_gross_ev=buy_gross_ev * 100,
            sell_gross_ev=sell_gross_ev * 100,
            all_gross_ev=all_gross_ev * 100,
            buy_net_ev=buy_net_ev * 100,
            sell_net_ev=sell_net_ev * 100,
            all_net_ev=all_net_ev * 100,
            buy_vs_random=(buy_gross_ev - random_ev) * 100,
            sell_vs_random=(sell_gross_ev - random_ev) * 100,
            all_vs_random=(all_gross_ev - random_ev) * 100,
            buy_t_stat=float(buy_t),
            sell_t_stat=float(sell_t),
            all_t_stat=float(all_t),
            buy_win_rate=buy_wr,
            sell_win_rate=sell_wr,
            pattern_bullish_pct=bullish_pct,
            pattern_bearish_pct=bearish_pct,
            pattern_neutral_pct=neutral_pct,
        )

    def _calculate_returns(
        self,
        df: pd.DataFrame,
        indices: np.ndarray,
        action: str,
    ) -> np.ndarray:
        """Calculate forward returns for signal indices."""
        if len(indices) == 0:
            return np.array([])

        entry_prices = df.iloc[indices]["close"].values
        exit_prices = df.iloc[indices + self.config.forward_window]["close"].values

        returns = (exit_prices - entry_prices) / entry_prices

        if action == "SELL":
            returns = -returns

        return returns

    def _random_baseline(self, df: pd.DataFrame) -> np.ndarray:
        """Generate random entry baseline."""
        n = len(df) - self.config.forward_window
        if n <= 0:
            return np.array([0.0])

        # Sample random entry points
        sample_size = min(self.config.random_baseline_samples, n)
        random_idx = np.random.choice(n, sample_size, replace=False)

        entry_prices = df.iloc[random_idx]["close"].values
        exit_prices = df.iloc[random_idx + self.config.forward_window]["close"].values

        returns = (exit_prices - entry_prices) / entry_prices
        return returns

    def analyze_all(
        self,
        symbols: list[str],
        interval: str = "5minute",
        **kwargs,
    ) -> dict[str, EdgeResult]:
        """Analyze edge for multiple symbols."""
        results = {}
        for symbol in symbols:
            try:
                results[symbol] = self.analyze_symbol(symbol, interval, **kwargs)
            except Exception as e:
                logger.error("edge_analysis_failed", symbol=symbol, error=str(e))
        return results

    def print_summary(self, results: dict[str, EdgeResult]) -> None:
        """Print edge analysis summary."""
        print("\n" + "=" * 80)
        print("EDGE ANALYSIS SUMMARY")
        print("=" * 80)

        all_buy_ev = []
        all_sell_ev = []
        all_all_ev = []

        for symbol, result in results.items():
            print(f"\n{symbol}:")
            print(
                f"  Signals: BUY={result.buy_signals}, SELL={result.sell_signals}, Total={result.total_signals}"
            )
            print(
                f"  Gross EV: BUY={result.buy_gross_ev:+.4f}%, SELL={result.sell_gross_ev:+.4f}%, ALL={result.all_gross_ev:+.4f}%"
            )
            print(
                f"  Net EV:   BUY={result.buy_net_ev:+.4f}%, SELL={result.sell_net_ev:+.4f}%, ALL={result.all_net_ev:+.4f}%"
            )
            print(
                f"  vs Random: BUY={result.buy_vs_random:+.4f}%, SELL={result.sell_vs_random:+.4f}%, ALL={result.all_vs_random:+.4f}%"
            )
            print(
                f"  T-stat: BUY={result.buy_t_stat:+.2f}, SELL={result.sell_t_stat:+.2f}, ALL={result.all_t_stat:+.2f}"
            )
            print(f"  Win Rate: BUY={result.buy_win_rate:.1f}%, SELL={result.sell_win_rate:.1f}%")
            print(
                f"  Pattern: Bullish={result.pattern_bullish_pct:.1f}%, Bearish={result.pattern_bearish_pct:.1f}%, Neutral={result.pattern_neutral_pct:.1f}%"
            )

            if result.buy_signals >= self.config.min_trades:
                all_buy_ev.append(result.buy_net_ev)
            if result.sell_signals >= self.config.min_trades:
                all_sell_ev.append(result.sell_net_ev)
            if result.total_signals >= self.config.min_trades:
                all_all_ev.append(result.all_net_ev)

        if all_buy_ev:
            print(f"\n--- AGGREGATE (min {self.config.min_trades} trades) ---")
            print(
                f"  BUY Net EV:  {np.mean(all_buy_ev):+.4f}% (t={stats.ttest_1samp(all_buy_ev, 0).statistic:.2f})"
            )
            print(
                f"  SELL Net EV: {np.mean(all_sell_ev):+.4f}% (t={stats.ttest_1samp(all_sell_ev, 0).statistic:.2f})"
            )
            print(
                f"  ALL Net EV:  {np.mean(all_all_ev):+.4f}% (t={stats.ttest_1samp(all_all_ev, 0).statistic:.2f})"
            )


def diagnose_edge(
    symbols: list[str],
    interval: str = "5minute",
    **kwargs,
) -> dict[str, EdgeResult]:
    """Convenience function to diagnose edge."""
    diagnoser = EdgeDiagnoser()
    return diagnoser.analyze_all(symbols, interval, **kwargs)
