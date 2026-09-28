"""
Kite Connect has NO news API - business news comes from free public feeds.

Sources (all verified reachable; `python -m scripts.validate_setup` probes
every URL in FEEDS on your machine):
  Moneycontrol, Economic Times   - the two original feeds
  CNBC-TV18                      - ~200-entry market feed, high quality
  Livemint (companies+markets)   - broad market + company coverage
  Business Standard (markets+companies)
  NDTV Profit                    - feedburner "latest" feed
  BSE announcements.xml          - the exchange's corporate-announcement feed;
                                   titles carry the COMPANY NAME ("Tata
                                   Consultancy Services Ltd"), not the trading
                                   symbol, so they are tagged via a
                                   name->symbol map built from
                                   ingest/instruments.csv (Kite's dump lists
                                   both name and tradingsymbol)
  LiveSquawk / ScoutQuest        - NOT included: both are paid/closed feeds
                                   (LiveSquawk sells API access; ScoutQuest has
                                   no public feed - its headlines surface on
                                   Groww's stock-feed page under Groww's
                                   domain). Costs vs value in docs/STRATEGIES.md.

Headlines aren't pre-tagged to a symbol, so matching decides tagging: watchlist
symbols appear verbatim in wire headlines ("TCS"), BSE announcement titles use
company names, handled by the name map.
"""
import csv
import re
import time
from datetime import datetime

import feedparser
import requests

from paths import INSTRUMENTS_CSV
from storage.db import get_connection, get_watchlist_symbols

_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}
_REQUEST_TIMEOUT = 15
# Some hosts (BSE especially) rate-limit or drop bursts from one IP; a short
# pause between feeds keeps the whole pass reliable.
_SLEEP_BETWEEN_FEEDS = 0.5
# Cap per feed per run: the scheduler pass only needs fresh items, and an
# unbounded fetch (BSE publishes 16k announcements) would explode the first run
# and spam the sentiment budget.
MAX_ITEMS_PER_FEED = 60

# (source_tag, url). The tag is stored in the news.source column, so keep it
# short and stable - it doubles as the group-by key in dashboard queries.
FEEDS = [
    ("moneycontrol", "https://www.moneycontrol.com/rss/business.xml"),
    ("economictimes", "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/1977021501.cms"),
    ("cnbctv18", "https://www.cnbctv18.com/commonfeeds/v1/cne/rss/market.xml"),
    ("livemint", "https://www.livemint.com/rss/companies"),
    ("livemint-markets", "https://www.livemint.com/rss/markets"),
    ("business-standard", "https://www.business-standard.com/rss/markets-106.rss"),
    ("business-standard-co", "https://www.business-standard.com/rss/companies-101.rss"),
    ("ndtvprofit", "https://feeds.feedburner.com/ndtvprofit-latest"),
    ("bse-announcements", "https://www.bseindia.com/data/xml/announcements.xml"),
]

# Legal suffixes only. Do NOT strip industry words ("technologies",
# "industries", "india", ...) here: "TATA TECHNOLOGIES" and "TATA MOTORS"
# must stay distinguishable, and stripping their second word once collapsed
# both to "tata" - which mis-tagged every Tata headline as TATATECH.
# (Real bug caught by testing before shipping.) Kite's `name` column also
# truncates long names ("TATA CONSULTANCY SERV LT"), so some companies will
# simply not match from BSE titles - a miss is safe, a false tag is not.
_COMPANY_SUFFIX_RE = re.compile(
    r"\b(ltd|limited|lt|co|corp|corporation|company|inc|the)\b\.?",
    re.IGNORECASE,
)
# Normalized keys shorter than this are too generic to be trusted as
# substrings of arbitrary headlines ("acc" would live inside "according"
# without word boundaries, and 3-letter keys still collide in prose).
# Companies whose names normalize shorter (ACC, ITC, BEL) just won't tag
# from BSE announcements - they still tag from wire feeds via their symbol.
_MIN_NAME_KEY_LEN = 4


def _fetch_feed(url: str):
    """Downloads and parses one feed with browser headers. feedparser.fetch
    is avoided because plain feedparser.parse(url) sends a feedparser UA,
    which BSE (and occasionally Livemint) answers with 403s."""
    try:
        resp = requests.get(url, headers=_UA, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
    except Exception as e:
        print(f"  [warn] fetch failed for {url}: {type(e).__name__}: {e}")
        return []
    return feedparser.parse(resp.content).entries


def _clean(headline: str) -> str:
    return re.sub(r"\s+", " ", (headline or "")).strip()


def _entry_published(entry, fetched_at: str) -> str:
    for key in ("published", "updated", "created"):
        if entry.get(key):
            return entry[key]
    return fetched_at


def match_symbol(headline: str, watchlist_symbols: list) -> str | None:
    """Symbol match: the watchlist symbol appearing as a whole word in the
    headline. Word boundaries matter - 'ACC' must not match 'according'."""
    hl = f" {headline.lower()} "
    for symbol in watchlist_symbols:
        if re.search(rf"(?<![a-z0-9]){re.escape(symbol.lower())}(?![a-z0-9])", hl):
            return symbol
    return None


def build_company_name_map(watchlist_symbols: list) -> dict:
    """{normalized company name: tradingsymbol} restricted to the watchlist,
    from the instrument dump's `name` column. Names are normalized the same
    way BSE titles are (suffixes stripped, non-alphanumerics collapsed), so
    the two sides meet in the middle. Requires ingest/instruments.csv; if the
    dump is missing, BSE announcements simply go untagged rather than
    crashing the whole news pass."""
    wanted = {s.upper() for s in watchlist_symbols}
    name_map: dict[str, str] = {}
    try:
        with open(INSTRUMENTS_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("exchange") != "NSE" or row.get("segment") != "NSE" \
                        or row.get("instrument_type") != "EQ":
                    continue
                symbol = (row.get("tradingsymbol") or "").strip().upper()
                if symbol not in wanted:
                    continue
                name = (row.get("name") or "").strip()
                if not name:
                    continue
                # Kite lists renamed companies as "NEWNAME - OLDNAME" (e.g.
                # "ETERNAL - ZOMATO"). BSE titles will use either one, so
                # index each alias part separately.
                for part in re.split(r"\s+-\s+", name):
                    normalized = _normalize_company(part)
                    if len(normalized) < _MIN_NAME_KEY_LEN:
                        continue
                    name_map.setdefault(normalized, symbol)
    except FileNotFoundError:
        print("  [warn] instruments.csv not found - BSE announcements will not be symbol-tagged")
    return name_map


def _normalize_company(name: str) -> str:
    n = _COMPANY_SUFFIX_RE.sub(" ", name)
    n = re.sub(r"[^a-z0-9]+", " ", n.lower())
    return re.sub(r"\s+", " ", n).strip()


def match_symbol_by_name(headline: str, name_map: dict) -> str | None:
    """Company-name match for BSE announcement titles. Only the LONGEST
    matching name wins, so 'Tata Motors Finance' doesn't get claimed by
    'Tata Motors' (and vice versa depending on which key comes first)."""
    norm = f" {_normalize_company(headline)} "
    best, best_len = None, 0
    for name, symbol in name_map.items():
        padded = f" {name} "
        if len(name) > best_len and padded in norm:
            best, best_len = symbol, len(name)
    return best


def fetch_and_store_news() -> None:
    conn = get_connection()
    fetched_at = datetime.now().isoformat()
    watchlist_symbols = get_watchlist_symbols()  # fresh each run - watchlist changes apply without a restart
    name_map = build_company_name_map(watchlist_symbols)
    new_count = 0
    per_source: dict[str, int] = {}

    for source, url in FEEDS:
        entries = _fetch_feed(url)[:MAX_ITEMS_PER_FEED]
        stored_this_feed = 0
        for entry in entries:
            headline = _clean(entry.get("title", ""))
            if not headline:
                continue
            published = _entry_published(entry, fetched_at)
            symbol = match_symbol(headline, watchlist_symbols)
            if symbol is None and name_map:
                symbol = match_symbol_by_name(headline, name_map)

            cur = conn.execute(
                "INSERT OR IGNORE INTO news (symbol, headline, source, published_at, fetched_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (symbol, headline, source, published, fetched_at),
            )
            if cur.rowcount:
                new_count += 1
                stored_this_feed += 1
        per_source[source] = stored_this_feed
        conn.commit()  # commit per feed - a later feed's crash can't lose earlier items
        time.sleep(_SLEEP_BETWEEN_FEEDS)

    conn.close()
    summary = ", ".join(f"{s}: {c}" for s, c in per_source.items() if c)
    print(f"{new_count} new headlines stored ({summary or 'nothing new'})")


if __name__ == "__main__":
    fetch_and_store_news()
