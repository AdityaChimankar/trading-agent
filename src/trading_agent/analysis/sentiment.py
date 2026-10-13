"""Sentiment analysis for news and social media."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from trading_agent.core.logging_config import get_logger

logger = get_logger(__name__)


class SentimentLabel(Enum):
    """Sentiment labels."""

    VERY_BEARISH = "very_bearish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    BULLISH = "bullish"
    VERY_BULLISH = "very_bullish"


@dataclass(frozen=True)
class SentimentScore:
    """Sentiment score for a symbol."""

    symbol: str
    timestamp: datetime
    label: SentimentLabel
    score: float  # -1 to 1
    confidence: float  # 0-1
    source: str
    article_count: int
    key_phrases: list[str]


class SentimentAnalyzer:
    """Analyze sentiment from news and other sources."""

    def __init__(self):
        self._symbol_keywords: dict[str, list[str]] = {}

    def register_symbol_keywords(self, symbol: str, keywords: list[str]) -> None:
        """Register keywords for a symbol."""
        self._symbol_keywords[symbol] = [k.lower() for k in keywords]

    def analyze_text(self, text: str, symbol: str | None = None) -> SentimentScore:
        """Analyze sentiment of a text."""
        # Simple keyword-based sentiment (replace with actual NLP model)
        text_lower = text.lower()

        # Financial sentiment keywords
        bullish_words = [
            "bullish",
            "buy",
            "long",
            "upside",
            "rally",
            "surge",
            "jump",
            "gain",
            "profit",
            "growth",
            "positive",
            "beat",
            "exceed",
            "strong",
            "outperform",
            "upgrade",
            "target raised",
            "bullish",
            "optimistic",
            "confident",
        ]

        bearish_words = [
            "bearish",
            "sell",
            "short",
            "downside",
            "fall",
            "drop",
            "decline",
            "loss",
            "weak",
            "negative",
            "miss",
            "disappoint",
            "underperform",
            "downgrade",
            "target cut",
            "pessimistic",
            "concern",
            "risk",
            "volatile",
        ]

        bullish_count = sum(1 for w in bullish_words if w in text_lower)
        bearish_count = sum(1 for w in bearish_words if w in text_lower)

        total = bullish_count + bearish_count
        score = 0.0 if total == 0 else (bullish_count - bearish_count) / total

        # Determine label
        if score > 0.5:
            label = SentimentLabel.VERY_BULLISH
        elif score > 0.1:
            label = SentimentLabel.BULLISH
        elif score < -0.5:
            label = SentimentLabel.VERY_BEARISH
        elif score < -0.1:
            label = SentimentLabel.BEARISH
        else:
            label = SentimentLabel.NEUTRAL

        # Extract key phrases (simplified)
        key_phrases = []
        for w in bullish_words + bearish_words:
            if w in text_lower:
                key_phrases.append(w)

        return SentimentScore(
            symbol=symbol or "UNKNOWN",
            timestamp=datetime.now(UTC),
            label=label,
            score=score,
            confidence=min(total / 5.0, 1.0),
            source="text_analysis",
            article_count=1,
            key_phrases=key_phrases[:5],
        )

    def analyze_articles(
        self,
        articles: list[dict],
        symbol: str | None = None,
    ) -> list[SentimentScore]:
        """Analyze sentiment for multiple articles."""
        results = []
        for article in articles:
            text = f"{article.get('title', '')} {article.get('summary', '')}"
            sentiment = self.analyze_text(text, symbol)
            sentiment = SentimentScore(
                symbol=sentiment.symbol,
                timestamp=sentiment.timestamp,
                label=sentiment.label,
                score=sentiment.score,
                confidence=sentiment.confidence,
                source=article.get("source", "unknown"),
                article_count=1,
                key_phrases=sentiment.key_phrases,
            )
            results.append(sentiment)
        return results

    def aggregate_sentiment(
        self,
        sentiments: list[SentimentScore],
    ) -> SentimentScore | None:
        """Aggregate multiple sentiment scores."""
        if not sentiments:
            return None

        # Weight by confidence
        weighted_score = sum(s.score * s.confidence for s in sentiments)
        total_confidence = sum(s.confidence for s in sentiments)

        if total_confidence == 0:
            return None

        avg_score = weighted_score / total_confidence
        avg_confidence = total_confidence / len(sentiments)

        # Determine label
        if avg_score > 0.5:
            label = SentimentLabel.VERY_BULLISH
        elif avg_score > 0.1:
            label = SentimentLabel.BULLISH
        elif avg_score < -0.5:
            label = SentimentLabel.VERY_BEARISH
        elif avg_score < -0.1:
            label = SentimentLabel.BEARISH
        else:
            label = SentimentLabel.NEUTRAL

        # Combine key phrases
        all_phrases = []
        for s in sentiments:
            all_phrases.extend(s.key_phrases)

        return SentimentScore(
            symbol=sentiments[0].symbol,
            timestamp=datetime.now(UTC),
            label=label,
            score=avg_score,
            confidence=avg_confidence,
            source="aggregated",
            article_count=len(sentiments),
            key_phrases=list(set(all_phrases))[:10],
        )

    def get_symbol_sentiment(
        self,
        articles: list[dict],
        symbol: str,
    ) -> SentimentScore | None:
        """Get aggregated sentiment for a specific symbol."""
        # Filter articles mentioning symbol
        symbol_lower = symbol.lower()
        relevant = []

        for article in articles:
            text = f"{article.get('title', '')} {article.get('summary', '')}".lower()
            if symbol_lower in text or any(
                kw in text for kw in self._symbol_keywords.get(symbol, [])
            ):
                relevant.append(article)

        if not relevant:
            return None

        sentiments = self.analyze_articles(relevant, symbol)
        return self.aggregate_sentiment(sentiments)


# Global analyzer
_analyzer: SentimentAnalyzer | None = None


def get_sentiment_analyzer() -> SentimentAnalyzer:
    """Get global sentiment analyzer."""
    global _analyzer
    if _analyzer is None:
        _analyzer = SentimentAnalyzer()
    return _analyzer
