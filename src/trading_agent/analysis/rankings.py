"""Ranking and scoring utilities."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from trading_agent.core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class RankedSymbol:
    """A ranked symbol with score and metadata."""

    symbol: str
    score: float
    rank: int
    category: str  # "momentum", "mean_reversion", "breakout", "oversold", "overbought"
    signals: dict
    metrics: dict


def rank_by_momentum(
    symbols: list[str],
    returns: dict[str, float],
    top_n: int = 10,
) -> list[RankedSymbol]:
    """Rank symbols by momentum (recent returns)."""
    ranked = sorted(
        returns.items(),
        key=lambda x: x[1],
        reverse=True,
    )

    results = []
    for i, (symbol, ret) in enumerate(ranked[:top_n]):
        results.append(
            RankedSymbol(
                symbol=symbol,
                score=ret,
                rank=i + 1,
                category="momentum",
                signals={"momentum": "positive" if ret > 0 else "negative"},
                metrics={"return": ret},
            )
        )
    return results


def rank_by_mean_reversion(
    symbols: list[str],
    rsi_values: dict[str, float],
    bb_position: dict[str, float],
    top_n: int = 10,
) -> list[RankedSymbol]:
    """Rank symbols by mean reversion potential."""
    # Low RSI + lower Bollinger Band = oversold (buy candidate)
    # High RSI + upper Bollinger Band = overbought (sell candidate)

    scored = []
    for symbol in symbols:
        rsi = rsi_values.get(symbol, 50)
        bb_pos = bb_position.get(symbol, 0.5)

        if rsi < 30 and bb_pos < 0.2:
            score = (30 - rsi) / 30 + (0.2 - bb_pos) / 0.2
            category = "oversold"
        elif rsi > 70 and bb_pos > 0.8:
            score = (rsi - 70) / 30 + (bb_pos - 0.8) / 0.2
            category = "overbought"
        else:
            continue

        scored.append((symbol, score, category, rsi, bb_pos))

    scored.sort(key=lambda x: x[1], reverse=True)

    results = []
    for i, (symbol, score, category, rsi, bb_pos) in enumerate(scored[:top_n]):
        results.append(
            RankedSymbol(
                symbol=symbol,
                score=score,
                rank=i + 1,
                category=category,
                signals={"mean_reversion": category},
                metrics={"rsi": rsi, "bb_position": bb_pos},
            )
        )
    return results


def rank_by_volume_surge(
    symbols: list[str],
    volume_ratio: dict[str, float],
    price_change: dict[str, float],
    top_n: int = 10,
) -> list[RankedSymbol]:
    """Rank symbols by volume surge with price confirmation."""
    scored = []
    for symbol in symbols:
        vol_ratio = volume_ratio.get(symbol, 1.0)
        chg = price_change.get(symbol, 0.0)

        if vol_ratio > 2.0:  # 2x average volume
            score = min(vol_ratio / 5.0, 1.0) * (1 + abs(chg))
            category = "breakout" if chg > 0 else "breakdown"
            scored.append((symbol, score, category, vol_ratio, chg))

    scored.sort(key=lambda x: x[1], reverse=True)

    results = []
    for i, (symbol, score, category, vol_ratio, chg) in enumerate(scored[:top_n]):
        results.append(
            RankedSymbol(
                symbol=symbol,
                score=score,
                rank=i + 1,
                category=category,
                signals={"volume_surge": True},
                metrics={"volume_ratio": vol_ratio, "price_change_pct": chg * 100},
            )
        )
    return results


def combine_rankings(
    *rankings: list[RankedSymbol],
    weights: list[float] | None = None,
) -> list[RankedSymbol]:
    """Combine multiple rankings with weighted scores."""
    if weights is None:
        weights = [1.0] * len(rankings)

    # Aggregate scores by symbol
    symbol_scores: dict[str, float] = {}
    symbol_data: dict[str, dict] = {}

    for ranking, weight in zip(rankings, weights, strict=False):
        for item in ranking:
            if item.symbol not in symbol_scores:
                symbol_scores[item.symbol] = 0.0
                symbol_data[item.symbol] = {
                    "categories": [],
                    "signals": {},
                    "metrics": {},
                }
            symbol_scores[item.symbol] += item.score * weight
            symbol_data[item.symbol]["categories"].append(item.category)
            symbol_data[item.symbol]["signals"].update(item.signals)
            symbol_data[item.symbol]["metrics"].update(item.metrics)

    # Create combined ranking
    combined = sorted(symbol_scores.items(), key=lambda x: x[1], reverse=True)

    results = []
    for i, (symbol, score) in enumerate(combined):
        data = symbol_data[symbol]
        results.append(
            RankedSymbol(
                symbol=symbol,
                score=score,
                rank=i + 1,
                category=", ".join(set(data["categories"])),
                signals=data["signals"],
                metrics=data["metrics"],
            )
        )

    return results


def percentile_rank(values: dict[str, float]) -> dict[str, float]:
    """Convert values to percentile ranks (0-100)."""
    if not values:
        return {}

    sorted_items = sorted(values.items(), key=lambda x: x[1])
    n = len(sorted_items)

    ranks = {}
    for i, (symbol, _) in enumerate(sorted_items):
        ranks[symbol] = (i + 1) / n * 100

    return ranks


def z_score_rank(values: dict[str, float]) -> dict[str, float]:
    """Convert values to z-scores."""
    if not values:
        return {}

    vals = np.array(list(values.values()))
    mean = np.mean(vals)
    std = np.std(vals)

    if std == 0:
        return dict.fromkeys(values, 0.0)

    return {k: (v - mean) / std for k, v in values.items()}


def composite_score(
    symbols: list[str],
    factors: dict[str, dict[str, float]],
    weights: dict[str, float],
) -> list[RankedSymbol]:
    """Calculate composite score from multiple factors.

    Args:
        symbols: List of symbols to score
        factors: Dict of factor_name -> dict of symbol -> value
        weights: Dict of factor_name -> weight

    Returns:
        List of RankedSymbol sorted by composite score.
    """
    scores = {}
    for symbol in symbols:
        total = 0.0
        total_weight = 0.0
        metrics = {}

        for factor_name, weight in weights.items():
            if factor_name in factors and symbol in factors[factor_name]:
                value = factors[factor_name][symbol]
                total += value * weight
                total_weight += weight
                metrics[factor_name] = value

        if total_weight > 0:
            scores[symbol] = total / total_weight
        else:
            scores[symbol] = 0.0

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    return [
        RankedSymbol(
            symbol=symbol,
            score=score,
            rank=i + 1,
            category="composite",
            signals={},
            metrics={},
        )
        for i, (symbol, score) in enumerate(ranked)
    ]
