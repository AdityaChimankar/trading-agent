"""Technical summary generation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from trading_agent.core.logging_config import get_logger
from trading_agent.storage.parquet_store import get_parquet_store

logger = get_logger(__name__)


@dataclass(frozen=True)
class TechnicalSummary:
    """Technical analysis summary for a symbol."""

    symbol: str
    timestamp: datetime
    interval: str

    # Price
    current_price: float
    open_price: float
    high_price: float
    low_price: float
    change_pct: float

    # Trends
    trend: str  # "bullish", "bearish", "neutral"
    trend_strength: float  # 0-1

    # Indicators
    rsi: float
    rsi_signal: str  # "oversold", "overbought", "neutral"
    macd_signal: str  # "bullish", "bearish", "neutral"
    bb_position: float  # 0-1, position within Bollinger Bands
    bb_signal: str

    # Volume
    volume: int
    volume_ratio: float  # vs 20-period average
    volume_signal: str

    # Support/Resistance
    support_levels: list[float]
    resistance_levels: list[float]
    nearest_support: float | None
    nearest_resistance: float | None

    # Patterns
    pattern: str | None
    pattern_signal: str | None

    # Overall
    overall_signal: str  # "buy", "sell", "hold"
    confidence: float  # 0-1


class TechnicalAnalyzer:
    """Generate technical summaries."""

    def __init__(self, parquet_store=None):
        self.parquet = parquet_store or get_parquet_store()

    def analyze(
        self,
        symbol: str,
        interval: str = "5minute",
        lookback: int = 100,
    ) -> TechnicalSummary | None:
        """Generate technical summary for a symbol."""
        df = self.parquet.read_latest(symbol, interval, n=lookback)
        if df.empty or len(df) < 50:
            return None

        return self._analyze_dataframe(df, symbol, interval)

    def _analyze_dataframe(
        self,
        df: pd.DataFrame,
        symbol: str,
        interval: str,
    ) -> TechnicalSummary:
        """Analyze a DataFrame and generate summary."""
        last = df.iloc[-1]
        df.iloc[-2] if len(df) > 1 else last

        current_price = last["close"]
        open_price = last["open"]
        high_price = last["high"]
        low_price = last["low"]
        change_pct = (current_price - open_price) / open_price * 100

        # Calculate indicators
        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"]

        # RSI
        rsi = self._calculate_rsi(close)
        rsi_signal = self._rsi_signal(rsi)

        # MACD
        macd_signal = self._macd_signal(close)

        # Bollinger Bands
        bb_pos, bb_signal = self._bb_position(close)

        # Volume
        vol_ratio = volume.iloc[-1] / volume.rolling(20).mean().iloc[-1]
        volume_signal = self._volume_signal(vol_ratio)

        # Trend
        trend, trend_strength = self._determine_trend(close)

        # Support/Resistance
        support, resistance = self._find_levels(high, low, close)
        nearest_support = self._nearest_level(current_price, support, "below")
        nearest_resistance = self._nearest_level(current_price, resistance, "above")

        # Patterns
        pattern, pattern_signal = self._detect_pattern(df)

        # Overall signal
        overall_signal, confidence = self._overall_signal(
            rsi_signal, macd_signal, bb_signal, volume_signal, trend, pattern_signal, change_pct
        )

        return TechnicalSummary(
            symbol=symbol,
            timestamp=last["timestamp"],
            interval=interval,
            current_price=current_price,
            open_price=open_price,
            high_price=high_price,
            low_price=low_price,
            change_pct=change_pct,
            trend=trend,
            trend_strength=trend_strength,
            rsi=rsi,
            rsi_signal=rsi_signal,
            macd_signal=macd_signal,
            bb_position=bb_pos,
            bb_signal=bb_signal,
            volume=int(last["volume"]),
            volume_ratio=vol_ratio,
            volume_signal=volume_signal,
            support_levels=support,
            resistance_levels=resistance,
            nearest_support=nearest_support,
            nearest_resistance=nearest_resistance,
            pattern=pattern,
            pattern_signal=pattern_signal,
            overall_signal=overall_signal,
            confidence=confidence,
        )

    def _calculate_rsi(self, close: pd.Series, period: int = 14) -> float:
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(period).mean()
        loss = -delta.where(delta < 0, 0).rolling(period).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        return float(rsi.iloc[-1]) if not np.isnan(rsi.iloc[-1]) else 50.0

    def _rsi_signal(self, rsi: float) -> str:
        if rsi < 30:
            return "oversold"
        elif rsi > 70:
            return "overbought"
        return "neutral"

    def _macd_signal(self, close: pd.Series) -> str:
        ema12 = close.ewm(span=12).mean()
        ema26 = close.ewm(span=26).mean()
        macd = ema12 - ema26
        signal = macd.ewm(span=9).mean()

        if macd.iloc[-1] > signal.iloc[-1] and macd.iloc[-2] <= signal.iloc[-2]:
            return "bullish"
        elif macd.iloc[-1] < signal.iloc[-1] and macd.iloc[-2] >= signal.iloc[-2]:
            return "bearish"
        return "neutral"

    def _bb_position(self, close: pd.Series, period: int = 20) -> tuple[float, str]:
        ma = close.rolling(period).mean()
        std = close.rolling(period).std()
        upper = ma + 2 * std
        lower = ma - 2 * std

        pos = (close.iloc[-1] - lower.iloc[-1]) / (upper.iloc[-1] - lower.iloc[-1])
        pos = max(0, min(1, pos))

        if pos > 0.8:
            signal = "upper_band"
        elif pos < 0.2:
            signal = "lower_band"
        else:
            signal = "neutral"

        return pos, signal

    def _volume_signal(self, ratio: float) -> str:
        if ratio > 2.0:
            return "high"
        elif ratio > 1.5:
            return "above_average"
        elif ratio < 0.5:
            return "low"
        return "normal"

    def _determine_trend(self, close: pd.Series) -> tuple[str, float]:
        ema9 = close.ewm(span=9).mean()
        ema21 = close.ewm(span=21).mean()

        if ema9.iloc[-1] > ema21.iloc[-1]:
            trend = "bullish"
            strength = min((ema9.iloc[-1] - ema21.iloc[-1]) / ema21.iloc[-1] * 100, 1.0)
        elif ema9.iloc[-1] < ema21.iloc[-1]:
            trend = "bearish"
            strength = min((ema21.iloc[-1] - ema9.iloc[-1]) / ema21.iloc[-1] * 100, 1.0)
        else:
            trend = "neutral"
            strength = 0.0

        return trend, strength

    def _find_levels(
        self,
        high: pd.Series,
        low: pd.Series,
        close: pd.Series,
        window: int = 20,
    ) -> tuple[list[float], list[float]]:
        # Find local highs and lows
        resistance = []
        support = []

        for i in range(window, len(close) - window):
            # Resistance: local high
            if high.iloc[i] == high.iloc[i - window : i + window + 1].max():
                resistance.append(high.iloc[i])

            # Support: local low
            if low.iloc[i] == low.iloc[i - window : i + window + 1].min():
                support.append(low.iloc[i])

        # Cluster nearby levels
        resistance = self._cluster_levels(resistance)
        support = self._cluster_levels(support)

        return support[-5:], resistance[-5:]  # Return last 5 levels

    def _cluster_levels(self, levels: list[float], threshold: float = 0.005) -> list[float]:
        """Cluster nearby price levels."""
        if not levels:
            return []

        levels = sorted(set(levels))
        clustered = []
        current_cluster = [levels[0]]

        for level in levels[1:]:
            if abs(level - current_cluster[-1]) / current_cluster[-1] < threshold:
                current_cluster.append(level)
            else:
                clustered.append(np.mean(current_cluster))
                current_cluster = [level]

        clustered.append(np.mean(current_cluster))
        return clustered

    def _nearest_level(self, price: float, levels: list[float], direction: str) -> float | None:
        """Find nearest support/resistance level."""
        if not levels:
            return None

        if direction == "below":
            candidates = [l for l in levels if l < price]
            return max(candidates) if candidates else None
        else:
            candidates = [l for l in levels if l > price]
            return min(candidates) if candidates else None

    def _detect_pattern(self, df: pd.DataFrame) -> tuple[str | None, str | None]:
        """Detect chart patterns (simplified)."""
        # This is a placeholder - real implementation would be more complex
        return None, None

    def _overall_signal(
        self,
        rsi_signal: str,
        macd_signal: str,
        bb_signal: str,
        volume_signal: str,
        trend: str,
        pattern_signal: str | None,
        change_pct: float,
    ) -> tuple[str, float]:
        """Determine overall signal."""
        bullish = 0
        bearish = 0

        if rsi_signal == "oversold":
            bullish += 1
        elif rsi_signal == "overbought":
            bearish += 1

        if macd_signal == "bullish":
            bullish += 1
        elif macd_signal == "bearish":
            bearish += 1

        if bb_signal == "lower_band":
            bullish += 1
        elif bb_signal == "upper_band":
            bearish += 1

        if volume_signal in ("high", "above_average") and change_pct > 0:
            bullish += 1
        elif volume_signal in ("high", "above_average") and change_pct < 0:
            bearish += 1

        if trend == "bullish":
            bullish += 1
        elif trend == "bearish":
            bearish += 1

        if bullish > bearish:
            return "buy", min(bullish / 6.0, 1.0)
        elif bearish > bullish:
            return "sell", min(bearish / 6.0, 1.0)
        return "hold", 0.0


def generate_summary_text(summary: TechnicalSummary) -> str:
    """Generate human-readable technical summary."""
    lines = [
        f"=== {summary.symbol} Technical Summary ===",
        f"Time: {summary.timestamp.strftime('%Y-%m-%d %H:%M')} ({summary.interval})",
        f"Price: {summary.current_price:.2f} | Open: {summary.open_price:.2f} | Change: {summary.change_pct:+.2f}%",
        "",
        f"Trend: {summary.trend.capitalize()} (strength: {summary.trend_strength:.2f})",
        f"RSI: {summary.rsi:.1f} ({summary.rsi_signal})",
        f"MACD: {summary.macd_signal.capitalize()}",
        f"Bollinger Bands: {summary.bb_signal.capitalize()} (position: {summary.bb_position:.2f})",
        f"Volume: {summary.volume:,} ({summary.volume_signal}, ratio: {summary.volume_ratio:.2f}x)",
        "",
    ]

    if summary.nearest_support:
        lines.append(f"Support: {summary.nearest_support:.2f}")
    if summary.nearest_resistance:
        lines.append(f"Resistance: {summary.nearest_resistance:.2f}")

    if summary.pattern:
        lines.append(f"Pattern: {summary.pattern} ({summary.pattern_signal})")

    lines.append("")
    lines.append(
        f"Overall Signal: {summary.overall_signal.upper()} (confidence: {summary.confidence:.2f})"
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Groww-style "Technicals" vote
#
# A SCANNABLE SUMMARY, not a validated signal: an equal-weighted vote across
# common indicators with no backtest behind the thresholds. Like the LLM and ML
# agents it sits beside the walk-forward-validated rule signal and never
# overrides it.
#
# Two conventions are deliberately mixed, as Groww does:
#   - REVERSAL (RSI, Stochastic, Williams %R): oversold = Bullish.
#   - MOMENTUM (MACD, CCI, ADX/DI, moving averages): direction continues.
# ---------------------------------------------------------------------------

# (min_net_score, label), net_score = bullish_count - bearish_count.
VERDICT_BANDS = [
    (5, "Strongly bullish"), (2, "Moderately bullish"),
    (-1, "Neutral"),
    (-4, "Moderately bearish"), (float("-inf"), "Strongly bearish"),
]


@dataclass(frozen=True)
class IndicatorReading:
    name: str
    value: float
    verdict: str  # "Bullish", "Bearish", or "Neutral"


def verdict_label(net_score: int) -> str:
    for threshold, label in VERDICT_BANDS:
        if net_score >= threshold:
            return label
    return "Neutral"


def technical_votes(df: pd.DataFrame) -> dict | None:
    """Bullish/Neutral/Bearish counts and verdict for the last candle of ``df``."""
    from trading_agent.core import quant_indicators as qi

    if len(df) < 30:
        return None
    h = df["high"].to_numpy(dtype=np.float64)
    lo = df["low"].to_numpy(dtype=np.float64)
    c = df["close"].to_numpy(dtype=np.float64)
    readings: list[IndicatorReading] = []

    def add(name: str, value: float, verdict: str) -> None:
        if np.isfinite(value):
            readings.append(IndicatorReading(name, round(float(value), 2), verdict))

    rsi = qi.rsi(c)[-1]
    add("RSI (14)", rsi, "Bullish" if rsi < 30 else "Bearish" if rsi > 70 else "Neutral")

    hist = qi.macd(c)[2][-1]
    add("MACD (12,26,9)", hist, "Bullish" if hist > 0 else "Bearish" if hist < 0 else "Neutral")

    k = qi.stochastic(h, lo, c)[0][-1]
    add("Stochastic %K", k, "Bullish" if k < 20 else "Bearish" if k > 80 else "Neutral")

    wr = qi.williams_r(h, lo, c)[-1]
    add("Williams %R", wr, "Bullish" if wr < -80 else "Bearish" if wr > -20 else "Neutral")

    cci = qi.cci(h, lo, c)[-1]
    add("CCI (20)", cci, "Bullish" if cci > 100 else "Bearish" if cci < -100 else "Neutral")

    plus_di, minus_di = (a[-1] for a in qi.directional_indicators(h, lo, c))
    adx = qi.adx(h, lo, c)[-1]
    if np.isfinite(plus_di) and np.isfinite(minus_di):
        # No confirmed trend below ADX 20, so direction isn't trustworthy.
        add("ADX/DI (14)", adx, "Neutral" if adx < 20 else "Bullish" if plus_di > minus_di else "Bearish")

    ma = {p: qi.sma(c, p)[-1] for p in (20, 50)}
    for period, value in ma.items():
        add(f"Price vs MA{period}", value, "Bullish" if c[-1] > value else "Bearish")
    if np.isfinite(ma[20]) and np.isfinite(ma[50]):
        add("MA20 vs MA50", ma[20] - ma[50], "Bullish" if ma[20] > ma[50] else "Bearish")

    if not readings:
        return None
    bullish = sum(r.verdict == "Bullish" for r in readings)
    bearish = sum(r.verdict == "Bearish" for r in readings)
    return {
        "readings": [r.__dict__ for r in readings],
        "bullish_count": bullish,
        "bearish_count": bearish,
        "neutral_count": len(readings) - bullish - bearish,
        "total": len(readings),
        "verdict": verdict_label(bullish - bearish),
    }


def get_technical_summary(symbol: str, interval: str = "5minute", candle_limit: int = 200,
                          parquet_store=None) -> dict | None:
    """Groww-style technicals for ``symbol`` from stored candles."""
    store = parquet_store or get_parquet_store()
    df = store.read_latest(symbol, interval, n=candle_limit)
    votes = technical_votes(df) if not df.empty else None
    if votes is None:
        return None
    return {"symbol": symbol, "interval": interval,
            "as_of": pd.Timestamp(df["timestamp"].iloc[-1]).isoformat(), **votes}
