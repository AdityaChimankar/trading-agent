"""
Single entrypoint for the automated part of the pipeline. Run this
alongside live_ticker.py during market hours.

  python -m ingest.live_ticker   (terminal 1 - live candle stream)
  python -m scripts.scheduler    (terminal 2 - this file)

LLM decision comparison runs less often than the rule-based cycle
(every 15 min, not 5) to control cost - it's a comparison tool, not
the thing making live calls, so it doesn't need the same frequency.

Two things here exist purely so the pipeline cannot go quiet without anyone
noticing:

- Every cycle is wrapped in a market-hours guard. The cron windows are
  hour-wide, so without this the jobs also fire before the open and after the
  close, computing signals from the previous session's candles and writing
  them as if they were today's.
- `heal_if_feed_down` watches the ticker's heartbeat and repairs the candles
  from Kite's historical endpoint when the live stream isn't delivering. If
  live_ticker dies, the day's data no longer stops at the moment it died -
  the hole gets filled in while the session is still running.
"""
import functools
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

# Explicit trigger objects do not inherit the scheduler timezone.
CronTrigger = functools.partial(CronTrigger, timezone="Asia/Kolkata")

from trading_agent.analysis.digest import DigestGenerator
from trading_agent.analysis.sentiment import get_sentiment_analyzer
from trading_agent.core.freshness import in_market_hours
from trading_agent.core.logging_config import get_logger
from trading_agent.ingest.fetch_news import fetch_latest_news
from trading_agent.ingest.watchlist_resolved import get_watchlist_symbols
from trading_agent.risk.position_monitor import run_monitor_cycle as monitor_positions
from trading_agent.strategy.decision_agent import run_decision_cycle as decide_symbols
from trading_agent.strategy.llm_decision_agent import run_llm_decision_cycle as decide_symbols_llm
from trading_agent.strategy.ml_decision_agent import run_ml_decision_cycle as decide_symbols_ml
from trading_agent.strategy.shadow import run_shadow_cycle as run_shadow_symbols
from trading_agent.scripts.ml_retrain import run_scheduled_job as ml_retrain_scheduled

logger = get_logger(__name__)


async def fetch_and_store_news():
    """Fetch latest news and store to database."""
    symbols = get_watchlist_symbols()
    articles = await fetch_latest_news(symbols=symbols[:50])
    logger.info("fetch_and_store_news", count=len(articles))


def flush_old_news(days: int = 3):
    """Flush old news from database (placeholder)."""
    logger.info("flush_old_news", days=days)


def fetch_news_cycle() -> None:
    """Run the asynchronous news job from APScheduler."""
    import asyncio

    asyncio.run(fetch_and_store_news())


def run_sentiment_pass():
    """Fetch news and analyze sentiment for watchlist symbols."""
    import asyncio

    symbols = get_watchlist_symbols()
    if not symbols:
        logger.warning("run_sentiment_pass: no symbols in watchlist")
        return

    try:
        # Fetch latest news
        articles = asyncio.run(fetch_latest_news(symbols=symbols[:50]))
        if not articles:
            logger.info("run_sentiment_pass: no articles fetched")
            return

        # Analyze sentiment per symbol
        analyzer = get_sentiment_analyzer()
        for symbol in symbols[:50]:
            sentiment = analyzer.get_symbol_sentiment(
                [
                    {
                        "title": a.title,
                        "summary": a.summary,
                        "source": a.source,
                    }
                    for a in articles
                ],
                symbol,
            )
            if sentiment:
                logger.info(
                    "sentiment_analyzed",
                    symbol=symbol,
                    label=sentiment.label.value,
                    score=round(sentiment.score, 3),
                    confidence=round(sentiment.confidence, 3),
                )
    except Exception as e:
        logger.error("run_sentiment_pass failed", error=str(e))


def generate_digest():
    """Generate and log daily digest."""
    symbols = get_watchlist_symbols()
    generator = DigestGenerator(symbols=symbols)
    digest = generator.generate()
    print(generator.format_digest(digest))
    logger.info("daily_digest_generated", opportunities=len(digest.top_opportunities))


async def run_decision_cycle():
    """Run rule-based decision cycle for all symbols."""
    symbols, interval = _fresh_decision_inputs()
    if symbols:
        await decide_symbols(symbols, interval=interval)


async def run_llm_decision_cycle():
    """Run LLM decision cycle for all symbols."""
    symbols, interval = _fresh_decision_inputs()
    if symbols:
        await decide_symbols_llm(symbols, interval=interval)


async def run_ml_decision_cycle():
    """Run ML decision cycle for all symbols."""
    symbols, interval = _fresh_decision_inputs()
    if symbols:
        await decide_symbols_ml(symbols, interval=interval)


async def run_shadow_cycle():
    """Run shadow testing cycle."""
    symbols, interval = _fresh_decision_inputs()
    if symbols:
        await run_shadow_symbols(symbols, interval=interval)


def _fresh_decision_inputs():
    """Fail closed for scheduled decisions outside session or on old/future bars."""
    from trading_agent.core.config import get_settings
    from trading_agent.storage.parquet_store import get_parquet_store
    interval = get_settings().storage.candle_interval
    if not in_market_hours():
        return [], interval
    seconds = {"minute": 60, "1minute": 60, "5minute": 300, "15minute": 900}[interval]
    now = datetime.now(UTC)
    symbols = []
    for symbol in get_watchlist_symbols():
        frame = get_parquet_store().read_latest(symbol, interval, n=1)
        if frame.empty:
            continue
        age = (now - frame["timestamp"].iloc[-1]).total_seconds()
        if seconds <= age <= 2 * seconds + 60:
            symbols.append(symbol)
        else:
            logger.warning("scheduled_symbol_withheld", symbol=symbol, candle_age_seconds=age)
    return symbols, interval


async def run_monitor_cycle():
    """Run position monitor cycle."""
    result = await monitor_positions()
    logger.info("run_monitor_cycle_completed", **result)


def decide_cycle():
    """Sync wrapper for run_decision_cycle."""
    import asyncio
    asyncio.run(run_decision_cycle())


def ml_decision_cycle():
    """Sync wrapper for run_ml_decision_cycle."""
    import asyncio
    asyncio.run(run_ml_decision_cycle())


def llm_decision_cycle():
    """Sync wrapper for run_llm_decision_cycle."""
    import asyncio
    asyncio.run(run_llm_decision_cycle())


def shadow_cycle():
    """Sync wrapper for run_shadow_cycle."""
    import asyncio
    asyncio.run(run_shadow_cycle())


def monitor_cycle():
    """Sync wrapper for run_monitor_cycle."""
    import asyncio
    asyncio.run(run_monitor_cycle())


def heal_if_feed_down():
    """Check if live feed is down and heal from historical endpoint."""
    from trading_agent.core.freshness import ingest_heartbeat
    heartbeat = ingest_heartbeat()
    if in_market_hours() and (not heartbeat or not heartbeat.get("connected")):
        end_of_session_heal()


def end_of_session_heal():
    """End of session gap healing."""
    import asyncio
    from trading_agent.core.config import get_settings
    from trading_agent.ingest.gap_healer import GapHealer
    healer = GapHealer(get_watchlist_symbols(), get_settings().storage.candle_interval)
    asyncio.run(healer.heal_to_current())
    if healer.errors:
        raise RuntimeError(f"Historical repair failed for {len(healer.errors)} symbols")


# Create scheduler AFTER all functions are defined
scheduler = BlockingScheduler(timezone="Asia/Kolkata")


# Weekly ML model retraining with drift detection.
def ml_retrain_cycle():
    """Weekly ML model retraining with drift detection.
    
    Runs outside market hours to avoid contention with live trading.
    """
    logger.info("Starting weekly ML retraining cycle...")
    try:
        result = ml_retrain_scheduled()
        logger.info(f"ML retraining cycle completed: {result}")
    except Exception as e:
        logger.error("ML retraining cycle failed", extra={"error": str(e)})


# Add jobs to scheduler
scheduler.add_job(
    fetch_news_cycle,
    CronTrigger(day_of_week="mon-fri", hour="9-15", minute="*/5"),
    id="fetch_news",
)
scheduler.add_job(
    run_sentiment_pass,
    CronTrigger(day_of_week="mon-fri", hour="9-15", minute="1-59/5"),
    id="score_sentiment",
)
scheduler.add_job(
    decide_cycle,
    CronTrigger(day_of_week="mon-fri", hour="9-15", minute="2-59/5"),
    id="decide",
)
# ML comparison - same 5-min cadence as rule-based decisions, since
# predictions are local/fast with no per-call cost like the LLM path
scheduler.add_job(
    ml_decision_cycle,
    # Shifted from "2-59/5" to "3-59/5" to prevent SQLite deadlocks
    CronTrigger(day_of_week="mon-fri", hour="9-15", minute="3-59/5"),
    id="ml_decide",
)
# LLM comparison - every 15 min, offset so news/sentiment are already fresh
scheduler.add_job(
    llm_decision_cycle,
    CronTrigger(day_of_week="mon-fri", hour="9-15", minute="3-59/15"),
    id="llm_decide",
)
# Candidate-strategy shadow logging - same 5-min cadence, offset last so it
# never contends with the other decision cycles' writes. Logs to the separate
# strategy_signals table only; never influences any live decision.
scheduler.add_job(
    shadow_cycle,
    CronTrigger(day_of_week="mon-fri", hour="9-15", minute="4-59/5"),
    id="shadow_candidates",
)
# Exit watch - every minute, because a stop is the one signal that is
# worthless late, and the decision cycles only run every 5. It reads candles
# and writes at most a handful of alert rows, so it does not need the
# off-cadence staggering the decision jobs use to avoid the write lock.
scheduler.add_job(
    monitor_cycle,
    CronTrigger(day_of_week="mon-fri", hour="9-15", minute="*"),
    id="monitor_positions",
)
# Feed watchdog - off-cadence from every decision job so it never competes
# with them for the write lock.
scheduler.add_job(
    heal_if_feed_down,
    CronTrigger(day_of_week="mon-fri", hour="9-15", minute="5-59/10"),
    id="heal_if_feed_down",
)
# Post-close repair, then the digest that depends on the repaired data.
scheduler.add_job(
    end_of_session_heal,
    CronTrigger(day_of_week="mon-fri", hour=15, minute=32),
    id="end_of_session_heal",
)
scheduler.add_job(
    generate_digest,
    CronTrigger(day_of_week="mon-fri", hour=15, minute=35),
    id="daily_digest",
)
# Daily news flush - 8:00 AM on weekdays (before market open)
scheduler.add_job(
    lambda: flush_old_news(days=3),
    CronTrigger(day_of_week="mon-fri", hour=8, minute=0),
    id="flush_old_news",
)
# Daily pre-flight validation - 8:15 AM on weekdays (before market open)
# Runs validation and logs results; doesn't stop the scheduler if it fails
scheduler.add_job(
    lambda: subprocess.run(
        [sys.executable, "-m", "trading_agent.scripts.validate_setup"],
        capture_output=True,
        text=True,
        check=False,
    ),
    CronTrigger(day_of_week="mon-fri", hour=8, minute=15),
    id="daily_validation",
)
# Weekly ML model retraining - Sunday 2 AM (outside market hours)
scheduler.add_job(
    ml_retrain_cycle,
    CronTrigger(day_of_week="sun", hour=2, minute=0),
    id="ml_retrain_weekly",
)


def _on_job_event(event):
    """Surface every dropped or failed run. APScheduler's default behaviour is
    to log and carry on, which is how a session loses cycles without anyone
    finding out until the results look thin."""
    detail = getattr(event, "exception", None) or getattr(event, "code", "")
    logger.warning(
        f"scheduler job event: {event.job_id} code={event.code}",
        extra={"job_id": event.job_id, "event_code": event.code, "detail": str(detail)},
    )


scheduler.add_listener(
    _on_job_event,
    EVENT_JOB_ERROR | EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES,
)


def run_once_now():
    fetch_news_cycle()
    run_sentiment_pass()
    decide_cycle()
    ml_decision_cycle()
    shadow_cycle()


def _shutdown() -> None:
    """Graceful shutdown handler for the scheduler."""
    logger.info("Shutdown initiated - stopping scheduler")
    try:
        scheduler.shutdown(wait=True)
    except Exception as e:
        logger.error("Error stopping scheduler", extra={"error": str(e)})


def main() -> None:
    """Start the scheduled pipeline and block until shutdown."""
    from trading_agent.core.logging_config import setup_logging
    from trading_agent.core.shutdown import register_shutdown_handler
    setup_logging()

    # Register shutdown handler
    register_shutdown_handler(_shutdown)

    logger.info("Running startup pass...")
    run_once_now()
    logger.info(
        "Scheduler started - news/sentiment/decisions/ML compare every 5 min, "
        "LLM comparison every 15 min, feed watchdog every 10 min, "
        "news flush daily at 8:00 AM, "
        "repair sweep at 3:32 PM, digest at 3:35 PM, "
        "weekly ML retraining Sunday 2 AM..."
    )
    scheduler.start()


if __name__ == "__main__":
    main()
