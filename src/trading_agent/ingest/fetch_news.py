"""News fetching for sentiment analysis."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

import feedparser
import httpx

from trading_agent.core.circuit_breaker import get_circuit_breaker
from trading_agent.core.exceptions import DataError
from trading_agent.core.logging_config import get_logger

logger = get_logger(__name__)

# Kite Connect has no news API, so headlines come from free market RSS feeds.
DEFAULT_RSS_FEEDS = [
    "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "https://www.moneycontrol.com/rss/latestnews.xml",
    "https://www.business-standard.com/rss/markets-106.rss",
    "https://www.livemint.com/rss/markets",
]


@dataclass(frozen=True)
class NewsArticle:
    """News article structure."""

    title: str
    summary: str
    url: str
    source: str
    published: datetime
    symbols: list[str]
    sentiment: float | None = None
    hash: str = ""

    def __post_init__(self):
        if not self.hash:
            content = f"{self.title}{self.summary}{self.url}"
            object.__setattr__(self, "hash", hashlib.sha256(content.encode()).hexdigest()[:16])


class NewsFetcher:
    """Fetch financial news from multiple sources."""

    def __init__(self):
        self.circuit_breakers = {
            "newsapi": get_circuit_breaker("newsapi"),
            "alpha_vantage": get_circuit_breaker("alpha_vantage"),
            "rss": get_circuit_breaker("news_rss"),
        }
        self._client: httpx.AsyncClient | None = None
        self._seen_hashes: set[str] = set()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def fetch_newsapi(
        self,
        api_key: str,
        query: str = "NSE OR India stock market",
        language: str = "en",
        page_size: int = 50,
    ) -> list[NewsArticle]:
        """Fetch from NewsAPI.org."""
        cb = self.circuit_breakers["newsapi"]

        async def _fetch():
            client = await self._get_client()
            url = "https://newsapi.org/v2/everything"
            params = {
                "q": query,
                "language": language,
                "pageSize": page_size,
                "sortBy": "publishedAt",
                "apiKey": api_key,
            }
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()

        try:
            data = await cb.call(_fetch)
            articles = []
            for item in data.get("articles", []):
                published = datetime.fromisoformat(item["publishedAt"].replace("Z", "+00:00"))
                article = NewsArticle(
                    title=item["title"] or "",
                    summary=item["description"] or "",
                    url=item["url"],
                    source=item["source"]["name"],
                    published=published,
                    symbols=self._extract_symbols(
                        item["title"] + " " + (item["description"] or "")
                    ),
                )
                if article.hash not in self._seen_hashes:
                    self._seen_hashes.add(article.hash)
                    articles.append(article)
            return articles
        except Exception as e:
            logger.error("newsapi_fetch_failed", error=str(e))
            raise DataError(f"NewsAPI fetch failed: {e}") from e

    async def fetch_alpha_vantage(
        self,
        api_key: str,
        symbols: list[str] | None = None,
    ) -> list[NewsArticle]:
        """Fetch from Alpha Vantage."""
        cb = self.circuit_breakers["alpha_vantage"]

        async def _fetch():
            client = await self._get_client()
            url = "https://www.alphavantage.co/query"
            params = {
                "function": "NEWS_SENTIMENT",
                "apikey": api_key,
            }
            if symbols:
                params["tickers"] = ",".join(symbols)
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()

        try:
            data = await cb.call(_fetch)
            articles = []
            for item in data.get("feed", []):
                published = datetime.strptime(item["time_published"], "%Y%m%dT%H%M%S").replace(
                    tzinfo=UTC
                )
                article = NewsArticle(
                    title=item["title"],
                    summary=item["summary"],
                    url=item["url"],
                    source="Alpha Vantage",
                    published=published,
                    symbols=item.get("ticker_sentiment", []),
                    sentiment=float(item.get("overall_sentiment_score", 0)),
                )
                if article.hash not in self._seen_hashes:
                    self._seen_hashes.add(article.hash)
                    articles.append(article)
            return articles
        except Exception as e:
            logger.error("alpha_vantage_fetch_failed", error=str(e))
            raise DataError(f"Alpha Vantage fetch failed: {e}") from e

    async def fetch_rss_feeds(
        self,
        feeds: list[str] | None = None,
    ) -> list[NewsArticle]:
        """Fetch from RSS feeds."""
        cb = self.circuit_breakers["rss"]

        feeds = feeds or DEFAULT_RSS_FEEDS
        articles = []

        for feed_url in feeds:
            try:

                async def _parse():
                    return feedparser.parse(feed_url)

                data = await cb.call(_parse)

                for entry in data.entries[:20]:  # Limit per feed
                    published = datetime.now(UTC)
                    if hasattr(entry, "published_parsed") and entry.published_parsed:
                        published = datetime(*entry.published_parsed[:6], tzinfo=UTC)

                    title = entry.get("title", "")
                    summary = entry.get("summary", entry.get("description", ""))

                    article = NewsArticle(
                        title=title,
                        summary=summary,
                        url=entry.get("link", ""),
                        source=data.feed.get("title", "RSS"),
                        published=published,
                        symbols=self._extract_symbols(title + " " + summary),
                    )

                    if article.hash not in self._seen_hashes:
                        self._seen_hashes.add(article.hash)
                        articles.append(article)

            except Exception as e:
                logger.warning("rss_feed_failed", feed=feed_url, error=str(e))

        return articles

    def _extract_symbols(self, text: str) -> list[str]:
        """Extract stock symbols from text."""
        # Simple extraction - could be enhanced with NER
        import re

        # Match common Indian stock symbols (uppercase, 2-15 chars)
        symbols = re.findall(r"\b[A-Z]{2,15}\b", text.upper())
        # Filter common false positives
        stopwords = {
            "THE",
            "AND",
            "FOR",
            "ARE",
            "BUT",
            "NOT",
            "YOU",
            "ALL",
            "CAN",
            "HER",
            "WAS",
            "ONE",
            "OUR",
            "OUT",
            "DAY",
            "GET",
            "HAS",
            "HIM",
            "HIS",
            "HOW",
            "ITS",
            "NEW",
            "NOW",
            "OLD",
            "SEE",
            "TWO",
            "WHO",
            "BOY",
            "DID",
            "MAN",
            "PUT",
            "SAY",
            "SHE",
            "TOO",
            "USE",
            "NSE",
            "BSE",
            "INDIA",
            "STOCK",
            "MARKET",
            "SHARE",
            "PRICE",
            "TRADE",
            "BUY",
            "SELL",
            "HIGH",
            "LOW",
            "OPEN",
            "CLOSE",
            "VOLUME",
            "INDEX",
            "NIFTY",
            "SENSEX",
            "BANK",
            "LTD",
            "LIMITED",
        }
        return [s for s in symbols if s not in stopwords and len(s) >= 3][:10]

    async def fetch_all(
        self,
        newsapi_key: str | None = None,
        alpha_vantage_key: str | None = None,
        rss_feeds: list[str] | None = None,
        symbols: list[str] | None = None,
    ) -> list[NewsArticle]:
        """Fetch from all configured sources."""
        tasks = []

        if newsapi_key:
            tasks.append(self.fetch_newsapi(newsapi_key))

        if alpha_vantage_key:
            tasks.append(self.fetch_alpha_vantage(alpha_vantage_key, symbols))

        tasks.append(self.fetch_rss_feeds(rss_feeds))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_articles = []
        for result in results:
            if isinstance(result, list):
                all_articles.extend(result)
            elif isinstance(result, Exception):
                logger.error("news_fetch_error", error=str(result))

        # Sort by published date, newest first
        all_articles.sort(key=lambda a: a.published, reverse=True)
        return all_articles


# Convenience function
async def fetch_latest_news(
    newsapi_key: str | None = None,
    alpha_vantage_key: str | None = None,
    symbols: list[str] | None = None,
) -> list[NewsArticle]:
    """Fetch latest news from all sources."""
    fetcher = NewsFetcher()
    try:
        return await fetcher.fetch_all(newsapi_key, alpha_vantage_key, symbols=symbols)
    finally:
        await fetcher.close()
