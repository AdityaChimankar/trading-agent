"""
Single entrypoint for the automated part of the pipeline. Run this
alongside live_ticker.py during market hours.

  python -m ingest.live_ticker   (terminal 1 - live candle stream)
  python -m scripts.scheduler    (terminal 2 - this file)

LLM decision comparison runs less often than the rule-based cycle
(every 15 min, not 5) to control cost - it's a comparison tool, not
the thing making live calls, so it doesn't need the same frequency.
"""
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from analysis.digest import generate_digest
from analysis.sentiment import run_sentiment_pass
from ingest.fetch_news import fetch_and_store_news
from storage.db import get_watchlist_symbols
from strategy.decision_agent import run_decision_cycle
from strategy.llm_decision_agent import run_llm_decision_cycle
from strategy.ml_decision_agent import run_ml_decision_cycle
from strategy.shadow import run_shadow_cycle

scheduler = BlockingScheduler()


def run_ml_decision_cycle_safe():
    """ML predictions are fast (no network calls, unlike the LLM
    comparison), so this runs on the same 5-min cadence as the
    rule-based decisions. Guarded because no model exists until
    train_ml_model.py has been run at least once - a fresh setup
    shouldn't crash the whole scheduler over a missing model file."""
    try:
        run_ml_decision_cycle(get_watchlist_symbols())
    except FileNotFoundError as e:
        print(f"ML decision cycle skipped: {e}")


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
    run_decision_cycle,
    CronTrigger(day_of_week="mon-fri", hour="9-15", minute="2-59/5"),
    id="decide",
)
# ML comparison - same 5-min cadence as rule-based decisions, since
# predictions are local/fast with no per-call cost like the LLM path
scheduler.add_job(
    run_ml_decision_cycle_safe,
    # Shifted from "2-59/5" to "3-59/5" to prevent SQLite deadlocks
    CronTrigger(day_of_week="mon-fri", hour="9-15", minute="3-59/5"),
    id="ml_decide",
)
# LLM comparison - every 15 min, offset so news/sentiment are already fresh
scheduler.add_job(
    lambda: run_llm_decision_cycle(get_watchlist_symbols()),
    CronTrigger(day_of_week="mon-fri", hour="9-15", minute="3-59/15"),
    id="llm_decide",
)
# Candidate-strategy shadow logging - same 5-min cadence, offset last so it
# never contends with the other decision cycles' writes. Logs to the separate
# strategy_signals table only; never influences any live decision.
scheduler.add_job(
    run_shadow_cycle,
    CronTrigger(day_of_week="mon-fri", hour="9-15", minute="4-59/5"),
    id="shadow_candidates",
)
# End-of-day digest, once, shortly after market close
scheduler.add_job(
    generate_digest,
    CronTrigger(day_of_week="mon-fri", hour=15, minute=35),
    id="daily_digest",
)


def run_once_now():
    fetch_and_store_news()
    run_sentiment_pass()
    run_decision_cycle()
    run_ml_decision_cycle_safe()
    run_shadow_cycle()


if __name__ == "__main__":
    print("Running startup pass...")
    run_once_now()
    print("Scheduler started - news/sentiment/decisions/ML compare every 5 min, "
          "LLM comparison every 15 min, digest at 3:35 PM...")
    scheduler.start()
