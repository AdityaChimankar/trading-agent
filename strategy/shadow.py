"""
Shadow agent - logs candidate strategies in parallel with the live rule-based
agent, into a SEPARATE table, without touching any live decision.

Same discipline as llm_signals / ml_signals: parallel logging, separate table,
zero influence on what is actually traded - until the track record justifies
otherwise. Every cycle, for every symbol, each registered candidate from
strategy/candidates.py evaluates the latest candle and its action is recorded
alongside the rule-based action at the same moment. After a few weeks this
table IS the live out-of-sample comparison that research/strategy_lab.py
cannot give you, because lab numbers are in-sample by construction.

The candidate functions are the EXACT same pure functions the lab backtests -
that parity is the whole point. If live and lab ever disagree, the bug is in
the plumbing (feature construction, candle alignment), not in the strategy.

Usage: python -m strategy.shadow            (one cycle over the whole watchlist)
"""
import numpy as np

from core.freshness import split_stale, warn_stale
from core.pattern_detection import detect_patterns
from core.quant_indicators import latest_indicators
from storage.db import get_connection, get_watchlist_symbols, init_db
from strategy.candidates import CANDIDATES, _swing_arrays
from strategy.decision_agent import evaluate_signals
from research.backtest import (
    prepare_symbol_series, precompute_pattern_bias_codes, vectorized_evaluate,
    LOOKBACK_MIN, SWING_ORDER,
)

# How many candles of history the live feature builder needs. Matches the
# lab's minimum so live and lab evaluate the same window shape.
MIN_CANDLES = max(200, LOOKBACK_MIN + SWING_ORDER + 10)


def _gate_context(symbol: str) -> dict | None:
    """Builds the prepared-arrays context for the LATEST candle of a symbol,
    using the same prepare_symbol_series() the lab uses - no reimplementation.
    Returns None when history is insufficient (log nothing rather than log
    a decision made on wrong data)."""
    prepared = prepare_symbol_series(symbol, candle_limit=MIN_CANDLES * 3)
    if prepared is None or prepared["length"] < MIN_CANDLES:
        return None
    prepared["swings_norm"] = _swing_arrays(prepared["swings"])
    return prepared


def evaluate_candidates_once(symbol: str) -> dict:
    """Returns {candidate_name: True/False} for the latest candle, or {}
    when the symbol lacks usable history. Pure evaluation - no DB writes."""
    prepared = _gate_context(symbol)
    if prepared is None:
        return {}
    i = prepared["length"] - 1
    # Swing gates need confirmation lag; the very last bar cannot have a
    # newly-confirmed swing, which matches how the lab measures it.
    return {name: bool(fn(prepared, i)) for name, fn in CANDIDATES.items()}


def run_shadow_cycle(symbols: list | None = None) -> None:
    """One parallel-logging pass over the watchlist. Never writes to
    `signals` - only to `strategy_signals`. Commits per symbol (same
    contention discipline as the other decision cycles)."""
    # Idempotent - guarantees the strategy_signals table exists even if the
    # DB on this machine predates the schema change and nothing else has
    # run init_db() since the upgrade.
    init_db()
    symbols = symbols or get_watchlist_symbols()
    # Same freshness gate as the decision cycles: a vote cast on a stale
    # candle would corrupt the out-of-sample track record these candidates
    # are eventually promoted on.
    symbols, stale = split_stale(symbols)
    warn_stale(stale, "shadow cycle")
    conn = get_connection()
    for symbol in symbols:
        indicators = latest_indicators(symbol)
        if "error" in indicators or not indicators.get("timestamp"):
            conn.commit()
            continue

        votes = evaluate_candidates_once(symbol)
        if not votes:
            conn.commit()
            continue

        # The rule-based agent's action on the SAME candle, computed via the
        # same evaluate_signals() path decision_agent.py uses, for direct
        # comparison in queries. Sentiment is taken as neutral here because
        # the shadow table stores deterministic rule logic; join against the
        # `sentiment` table if you want the sentiment-conditioned view.
        rsi, adx = indicators.get("rsi"), indicators.get("adx")
        pattern_result = detect_patterns(symbol)
        rule_action, _ = evaluate_signals(
            rsi, adx, 0.0, pattern_result["bias"], pattern_result["patterns"],
        )

        atr = indicators.get("atr")
        timestamp = indicators["timestamp"]
        for name, fired in votes.items():
            conn.execute(
                "INSERT INTO strategy_signals "
                "(symbol, timestamp, strategy_name, action, fired, atr, rule_based_action) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (symbol, timestamp, name, "BUY" if fired else "HOLD",
                 1 if fired else 0, atr, rule_action),
            )
        conn.commit()
        fired_names = [n for n, f in votes.items() if f]
        tail = f" -> {', '.join(fired_names)}" if fired_names else ""
        print(f"{symbol}: shadow logged{tail}")
    conn.close()


if __name__ == "__main__":
    run_shadow_cycle()
