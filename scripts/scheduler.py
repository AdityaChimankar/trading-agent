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

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from analysis.digest import generate_digest
from analysis.sentiment import run_sentiment_pass
from core.freshness import in_market_hours, ingest_heartbeat
from ingest.fetch_news import fetch_and_store_news
from storage.db import get_watchlist_symbols
from strategy.decision_agent import run_decision_cycle
from strategy.llm_decision_agent import run_llm_decision_cycle
from strategy.ml_decision_agent import run_ml_decision_cycle
from strategy.shadow import run_shadow_cycle

# If the ticker's heartbeat is older than this, treat the live feed as down.
HEARTBEAT_TRUST_SEC = 90
# How far back the emergency repair sweeps when the feed is down.
FEED_DOWN_LOOKBACK_MIN = 45

JOB_DEFAULTS = {
    # One copy of a cycle at a time: these all write to SQLite, and running
    # two at once is how "database is locked" starts.
    "max_instances": 1,
    # If several fire times are missed while a cycle overruns, run once rather
    # than once per missed fire...
    "coalesce": True,
    # ...but give a missed run a real grace window before discarding it.
    # APScheduler's default is ONE SECOND: a cycle that overruns its slot, or
    # a machine that briefly sleeps, silently drops the run and the session
    # ends up with an unexplained hole in its signal coverage.
    "misfire_grace_time": 120,
}

scheduler = BlockingScheduler(job_defaults=JOB_DEFAULTS)


def market_hours_only(fn):
    """Skip a cycle outside 9:15-15:30 rather than compute from stale candles.

    The cron windows are hour-wide ('9-15'), which covers 9:00-9:14 and
    15:30-15:59 too. Running then is never useful and is actively misleading:
    the indicators resolve to the previous session's last candle, and the
    resulting signals look current.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not in_market_hours():
            print(f"[skip] {fn.__name__}: outside the 9:15-15:30 session")
            return None
        return fn(*args, **kwargs)
    return wrapper


@market_hours_only
def decide_cycle():
    run_decision_cycle()


@market_hours_only
def ml_decision_cycle():
    """Guarded because no model exists until train_ml_model.py has been run at
    least once - a fresh setup shouldn't crash the whole scheduler over a
    missing model file."""
    try:
        run_ml_decision_cycle(get_watchlist_symbols())
    except FileNotFoundError as e:
        print(f"ML decision cycle skipped: {e}")


@market_hours_only
def llm_decision_cycle():
    run_llm_decision_cycle(get_watchlist_symbols())


@market_hours_only
def shadow_cycle():
    run_shadow_cycle()


def heal_if_feed_down():
    """Repair this session's candles when the live ticker isn't delivering.

    Deliberately reads the heartbeat before doing anything: a healthy feed
    heals its own outages, and a blind sweep every cycle would spend hundreds
    of historical requests at Kite's ~3/sec limit for no reason. So this costs
    one small query while things are fine and only fetches when the feed is
    actually down (process dead, socket silent, or token expired).
    """
    if not in_market_hours():
        return
    heartbeat = ingest_heartbeat()
    if heartbeat and heartbeat["alive"]:
        return  # the ticker is alive; it repairs its own outages
    age = heartbeat["heartbeat_age_sec"] if heartbeat else None
    print(f"[heal] live feed is down (heartbeat age: {age}s) - "
          f"repairing the last {FEED_DOWN_LOOKBACK_MIN} minutes from Kite history")
    from ingest.gap_healer import heal_recent  # lazy - only this path needs a REST client
    try:
        print(f"[heal] {heal_recent(FEED_DOWN_LOOKBACK_MIN)}")
    except Exception as e:
        print(f"[heal] repair sweep failed: {e}")


def end_of_session_heal():
    """Definitive sweep after the close: check every symbol for the whole
    session and refill any hole, whatever caused it."""
    from ingest.gap_healer import heal_day
    try:
        print(f"[heal] end-of-session sweep: {heal_day()}")
    except Exception as e:
        print(f"[heal] end-of-session sweep failed: {e}")


scheduler.add_job(
    fetch_and_store_news,
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


def _on_job_event(event):
    """Surface every dropped or failed run. APScheduler's default behaviour is
    to log and carry on, which is how a session loses cycles without anyone
    finding out until the results look thin."""
    detail = getattr(event, "exception", None) or getattr(event, "code", "")
    print(f"[scheduler] job '{event.job_id}' event={event.code}: {detail}")


scheduler.add_listener(
    _on_job_event,
    EVENT_JOB_ERROR | EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES,
)


def run_once_now():
    fetch_and_store_news()
    run_sentiment_pass()
    decide_cycle()
    ml_decision_cycle()
    shadow_cycle()


if __name__ == "__main__":
    print("Running startup pass...")
    run_once_now()
    print("Scheduler started - news/sentiment/decisions/ML compare every 5 min, "
          "LLM comparison every 15 min, feed watchdog every 10 min, "
          "repair sweep at 3:32 PM, digest at 3:35 PM...")
    scheduler.start()
