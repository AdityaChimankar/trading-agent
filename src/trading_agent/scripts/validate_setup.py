"""
Pre-flight check - run this BEFORE the live ticker / scheduler / dashboard API
every trading day. Verifies Kite auth + market data, the DB and watchlist,
news RSS feeds, and the OpenRouter LLM are all actually working - so a dead
API key or an expired token surfaces here, not 5 minutes into market
hours with a silent failure buried in a scheduler log.

Usage: python -m trading_agent.scripts.validate_setup [--skip-llm]
Exit code 0 = all checks passed, safe to start the pipeline.
Exit code 1 = at least one check failed - fix before proceeding.
"""
import argparse
import asyncio
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
    ok = True
    for var in ("KITE_API_KEY", "KITE_API_SECRET"):
        if os.getenv(var):
            print(f"  {CHECK_MARK} {var} set")
        else:
            print(f"  {FAIL_MARK} {var} missing from .env")
            ok = False
    if os.getenv("LLM_API_KEY") or os.getenv("OPENROUTER_API_KEY"):
        print(f"  {CHECK_MARK} OpenRouter key set")
    else:
        print(f"  {WARN_MARK} No LLM_API_KEY/OPENROUTER_API_KEY - sentiment and LLM agent disabled")
    missing_totp = [v for v in ("KITE_TOTP_SECRET", "KITE_USER_ID", "KITE_PASSWORD") if not os.getenv(v)]
    if missing_totp:
        print(f"  {WARN_MARK} Auto-login disabled (missing {', '.join(missing_totp)}) - manual login needed daily")
    return ok


def check_kite_auth() -> bool:
    print("Checking Kite Connect authentication...")
    from trading_agent.ingest.kite_auth import get_kite_client

    if os.getenv("KITE_TOTP_SECRET"):
        try:
            from trading_agent.ingest.kite_auto_login import ensure_valid_token

            ensure_valid_token()
        except Exception as e:
            print(f"  {WARN_MARK} Auto-login failed ({e}); trying saved token")
    try:
        profile = get_kite_client().profile()
        print(f"  {CHECK_MARK} Kite auth valid for {profile.get('user_id', '?')}")
        return True
    except Exception as e:
        print(f"  {FAIL_MARK} Kite auth failed: {e}")
        print("        Run: trading-agent login")
        return False


def check_kite_data_access() -> bool:
    print("Checking Kite market data...")
    try:
        from trading_agent.ingest.kite_auth import get_kite_client

        quote = get_kite_client().quote(["NSE:INFY"])
        price = quote["NSE:INFY"]["last_price"]
        print(f"  {CHECK_MARK} Quote OK (INFY {price})")
        return True
    except Exception as e:
        print(f"  {FAIL_MARK} Quote failed: {e} (market data subscription active?)")
        return False


def check_database() -> bool:
    print("Checking database...")
    try:
        from trading_agent.storage.db import get_connection

        conn = get_connection()
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        mark = CHECK_MARK if mode.lower() == "wal" else WARN_MARK
        print(f"  {mark} SQLite journal_mode={mode}")
        return True
    except Exception as e:
        print(f"  {FAIL_MARK} Database unavailable: {e}")
        return False


def check_watchlist() -> bool:
    print("Checking watchlist...")
    try:
        from trading_agent.ingest.instrument_lookup import get_instrument_lookup
        from trading_agent.ingest.watchlist_resolved import get_watchlist_symbols

        symbols = get_watchlist_symbols()
        if not symbols:
            print(f"  {FAIL_MARK} Watchlist is empty - run: python -m trading_agent.ingest.build_nifty500_symbols")
            return False
        lookup = get_instrument_lookup()
        unresolved = [s for s in symbols if not lookup.get_token(s)]
        print(f"  {CHECK_MARK} Watchlist has {len(symbols)} symbol(s)")
        if unresolved:
            print(f"  {WARN_MARK} {len(unresolved)} without instrument token: {', '.join(unresolved[:10])}")
        return True
    except Exception as e:
        print(f"  {FAIL_MARK} Could not read watchlist: {e}")
        return False


def check_news_feeds() -> bool:
    print("Checking news RSS feeds...")
    from trading_agent.ingest.fetch_news import DEFAULT_RSS_FEEDS

    reachable = 0
    for feed_url in DEFAULT_RSS_FEEDS:
        feed = feedparser.parse(feed_url)
        if feed.entries:
            reachable += 1
            print(f"  {CHECK_MARK} {feed_url} - {len(feed.entries)} entries")
        else:
            print(f"  {WARN_MARK} {feed_url} - no entries ({feed.get('bozo_exception', 'empty')})")
    # One dead feed is tolerable; none means no news at all.
    return reachable > 0


def check_llm() -> bool:
    print("Checking OpenRouter LLM...")
    from trading_agent.core.config import get_settings
    from trading_agent.core.llm_client import LLMRequest, MockLLMClient, create_llm_client

    settings = get_settings().llm
    client = create_llm_client(tier="quick")
    if isinstance(client, MockLLMClient):
        print(f"  {WARN_MARK} LLM provider is mock - no sentiment or LLM agent output")
        return True

    async def ping():
        try:
            return await client.complete(LLMRequest(
                system_prompt="You are a health check.",
                user_prompt="Reply with exactly one word: OK",
                max_tokens=5,
                temperature=0.0,
            ))
        finally:
            await client.close()

    try:
        reply = asyncio.run(ping())
        print(f"  {CHECK_MARK} {settings.model} responding ({reply.latency_ms:.0f} ms, got '{reply.content[:20]}')")
        return True
    except Exception as e:
        print(f"  {FAIL_MARK} LLM call failed: {e}")
        print("        Check OPENROUTER_API_KEY at https://openrouter.ai/keys")
        return False


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--skip-llm", action="store_true", help="Don't spend an LLM call")
    args = parser.parse_args(argv)

    print("=" * 60)
    print("Pre-flight check - verifying all APIs before pipeline start")
    print("=" * 60)

    results = {"Environment variables": check_env_vars()}
    print()
    results["Kite authentication"] = check_kite_auth()
    print()
    if results["Kite authentication"]:
        results["Kite market data"] = check_kite_data_access()
        print()
    results["Database"] = check_database()
    print()
    results["Watchlist"] = check_watchlist()
    print()
    results["News feeds"] = check_news_feeds()
    if not args.skip_llm:
        print()
        results["OpenRouter LLM"] = check_llm()

    print()
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    for name, passed in results.items():
        print(f"  {CHECK_MARK if passed else FAIL_MARK} {name}")

    if all(results.values()):
        print("\nAll checks passed - safe to start the live ticker, scheduler and dashboard API")
        sys.exit(0)
    print("\nOne or more checks failed - fix the issues above before starting the pipeline.")
    sys.exit(1)


if __name__ == "__main__":
    main()
