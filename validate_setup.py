"""
Pre-flight check - run this BEFORE live_ticker.py / scheduler.py /
dashboard.py every trading day. Verifies Kite auth + API, news feeds,
Gemini API, and the DB/watchlist are all actually working - so a dead
API key or an expired token surfaces here, not 5 minutes into market
hours with a silent failure buried in a scheduler log.

Usage: python validate_setup.py
Exit code 0 = all checks passed, safe to start the pipeline.
Exit code 1 = at least one check failed - fix before proceeding.
"""
import os
import sys
import feedparser
from dotenv import load_dotenv

load_dotenv()

CHECK_MARK = "[OK]"
FAIL_MARK = "[FAIL]"
WARN_MARK = "[WARN]"


def check_env_vars() -> bool:
    print("Checking environment variables...")
    required = ["KITE_API_KEY", "KITE_API_SECRET", "GEMINI_API_KEY"]
    missing = [k for k in required if not os.getenv(k) or "your_" in os.getenv(k, "")]
    if missing:
        print(f"  {FAIL_MARK} Missing or placeholder values in .env: {missing}")
        return False
    print(f"  {CHECK_MARK} All required .env variables are set")
    return True


def check_kite_auth() -> bool:
    print("Checking Kite Connect authentication...")
    if not os.path.exists(".access_token"):
        print(f"  {FAIL_MARK} .access_token not found - run: python kite_auth.py")
        return False

    try:
        from kite_auth import get_kite_client
        kite = get_kite_client()
        profile = kite.profile()
        print(f"  {CHECK_MARK} Kite auth valid - logged in as {profile.get('user_name', 'unknown')}")
        return True
    except Exception as e:
        print(f"  {FAIL_MARK} Kite auth failed: {e}")
        print(f"        Access tokens expire daily (~7:30 AM IST) - re-run: python kite_auth.py")
        return False


def check_kite_data_access() -> bool:
    print("Checking Kite market data access...")
    try:
        from kite_auth import get_kite_client
        kite = get_kite_client()
        # NIFTY 50 index token - a stable, always-valid instrument to test against
        quote = kite.quote(["NSE:INFY"])
        if quote:
            print(f"  {CHECK_MARK} Live market data access working (test quote: NSE:INFY)")
            return True
        print(f"  {FAIL_MARK} Quote request returned empty - check your Kite Connect plan/subscription")
        return False
    except Exception as e:
        print(f"  {FAIL_MARK} Market data request failed: {e}")
        print(f"        If this says 'permission denied', your Kite Connect subscription "
              f"may not include market data access (needs the paid plan, not just order APIs)")
        return False


def check_watchlist() -> bool:
    print("Checking watchlist is populated...")
    try:
        from db import get_watchlist_symbols
        symbols = get_watchlist_symbols()
        if not symbols:
            print(f"  {FAIL_MARK} Watchlist is empty - run: python fetch_historical.py")
            return False
        print(f"  {CHECK_MARK} Watchlist has {len(symbols)} symbol(s)")
        return True
    except Exception as e:
        print(f"  {FAIL_MARK} Could not read watchlist: {e}")
        print(f"        Run: python db.py")
        return False


def check_news_feeds() -> bool:
    print("Checking news RSS feeds...")
    from fetch_news import FEEDS
    all_ok = True
    for feed_url in FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            if feed.entries:
                print(f"  {CHECK_MARK} {feed_url} - {len(feed.entries)} entries")
            else:
                print(f"  {WARN_MARK} {feed_url} - reachable but returned 0 entries")
        except Exception as e:
            print(f"  {FAIL_MARK} {feed_url} - failed: {e}")
            all_ok = False
    return all_ok


def check_gemini_api() -> bool:
    print("Checking Gemini API...")
    try:
        import google.generativeai as genai
        genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
        model = genai.GenerativeModel("gemini-flash-lite-latest")
        response = model.generate_content("Reply with exactly one word: OK")
        if response.text.strip():
            print(f"  {CHECK_MARK} Gemini API responding (got: '{response.text.strip()[:30]}')")
            return True
        print(f"  {FAIL_MARK} Gemini API returned an empty response")
        return False
    except Exception as e:
        print(f"  {FAIL_MARK} Gemini API call failed: {e}")
        print(f"        Check GEMINI_API_KEY is valid at https://ai.google.dev")
        return False


def main():
    print("=" * 60)
    print("Pre-flight check - verifying all APIs before pipeline start")
    print("=" * 60)

    results = {
        "Environment variables": check_env_vars(),
    }
    print()
    results["Kite authentication"] = check_kite_auth()
    print()
    if results["Kite authentication"]:
        results["Kite market data"] = check_kite_data_access()
        print()
    results["Watchlist"] = check_watchlist()
    print()
    results["News feeds"] = check_news_feeds()
    print()
    results["Gemini API"] = check_gemini_api()

    print()
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    for name, passed in results.items():
        mark = CHECK_MARK if passed else FAIL_MARK
        print(f"  {mark} {name}")

    if all(results.values()):
        print("\nAll checks passed - safe to start live_ticker.py / scheduler.py / dashboard.py")
        sys.exit(0)
    else:
        print("\nOne or more checks failed - fix the issues above before starting the pipeline.")
        sys.exit(1)


if __name__ == "__main__":
    main()
