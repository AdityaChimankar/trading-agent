"""
Kite Connect does NOT provide a news API - this pulls business news
via free RSS feeds instead. Run this on the same schedule as your
candle polling (every 1-5 min during market hours).

Headlines aren't pre-tagged to a symbol, so basic keyword matching
against your watchlist is used here. For production quality, this is
exactly the kind of matching an LLM call does much better - but do
that only for headlines that already look watchlist-relevant, to
keep API costs down.
"""
import feedparser
from datetime import datetime
from db import get_connection, get_watchlist_symbols

# Free financial news RSS feeds - add/swap sources as needed
FEEDS = [
    "https://www.moneycontrol.com/rss/business.xml",
    "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/1977021501.cms",
]


def match_symbol(headline: str, watchlist_symbols: list) -> str | None:
    for symbol in watchlist_symbols:
        if symbol.lower() in headline.lower():
            return symbol
    return None


def fetch_and_store_news():
    conn = get_connection()
    fetched_at = datetime.now().isoformat()
    watchlist_symbols = get_watchlist_symbols()  # read fresh each run - reflects watchlist changes without a restart
    new_count = 0

    for feed_url in FEEDS:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries:
            headline = entry.get("title", "")
            published = entry.get("published", fetched_at)
            symbol = match_symbol(headline, watchlist_symbols)

            cur = conn.execute(
                "INSERT OR IGNORE INTO news (symbol, headline, source, published_at, fetched_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (symbol, headline, feed_url, published, fetched_at),
            )
            if cur.rowcount:
                new_count += 1

    conn.commit()
    conn.close()
    print(f"{new_count} new headlines stored")


if __name__ == "__main__":
    fetch_and_store_news()
