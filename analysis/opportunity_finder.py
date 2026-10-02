"""
Ranks watchlist candidates by CONVICTION - how many of your independent
signals (technical score, ML model, LLM agent, sentiment) agree on the
same direction - rather than just the raw technical score alone.

IMPORTANT - what "conviction" is and isn't: this is NOT a prediction of
"maximum profit possible". No signal can honestly promise that - future
prices are unknown. It also isn't trying to size a bigger position for
a "better" setup: position_sizing.py deliberately risks a FIXED % of
capital per trade with a FIXED reward:risk ratio, so nearly every
suggested trade targets roughly the same rupee profit (that consistency
IS the point of risk-based sizing, not a limitation to work around).

What conviction actually measures: agreement. A setup where the
technical score, the ML model, the LLM agent, and sentiment all point
the same direction is more likely to have the FIXED profit target
actually realized than one where they disagree - that's the closest
honest proxy for "best opportunity" this system can offer. Contradicting
signals actively REDUCE conviction, the same philosophy already used by
rankings.py's contradiction check against live position P&L.

Also reports REALISTIC ROOM TO TARGET - the distance to the nearest
opposing swing high/low (resistance for a BUY, support for a SELL) -
alongside the fixed ATR-based take-profit. If the fixed target sits
beyond a real resistance/support wall, that's worth knowing; if there's
genuine room beyond it, that's a more promising setup than the fixed
target alone would suggest.

NEW: Now uses HybridStrategy (strategy/hybrid_strategy.py) to select the
best decision mode (RULE_ONLY, ML_ONLY, or RULE_ML_COMBINED) per symbol
via backtest comparison, then uses that mode's signal for the opportunity.
"""
from dataclasses import dataclass, field

import pandas as pd
from scipy.signal import find_peaks

from analysis.rankings import rank_watchlist_with_sizing
from core.quant_indicators import compute_atr, load_candles
from storage.db import get_connection, get_open_position_symbols
from strategy.fast_hybrid_strategy import FastHybridStrategy, StrategyMode, _indicator_cache, _sentiment_cache, warmup_caches, warmup_caches_for_symbols
from strategy.hybrid_strategy import find_best_mode

# Point weights for the conviction score - each confirming signal ADDS
# up to this many points (scaled by its own confidence/magnitude); each
# CONTRADICTING signal subtracts the same amount. Technical score is
# used as-is (not rescaled) so it stays consistent with what rankings.py
# already shows elsewhere.
ML_WEIGHT = 20.0
LLM_WEIGHT = 20.0
SENTIMENT_WEIGHT = 15.0
SENTIMENT_THRESHOLD = 0.15  # |sentiment| below this counts as neutral, not agreement/disagreement

# Cache for best mode per symbol (persists across calls within same process)
_best_mode_cache: dict[str, StrategyMode] = {}


def _get_best_mode(symbol: str, candle_limit: int = 5000, use_cache: bool = True) -> StrategyMode:
    """Get the best strategy mode for a symbol, using cache or computing via backtest."""
    if use_cache and symbol in _best_mode_cache:
        return _best_mode_cache[symbol]
    
    # Import here to avoid circular import
    from strategy.hybrid_strategy import find_best_mode
    
    # Compute best mode via backtest
    result = find_best_mode([symbol], candle_limit, use_cache=use_cache)
    best_mode = StrategyMode(result["best_mode"])
    _best_mode_cache[symbol] = best_mode
    return best_mode


def _get_hybrid_signal(symbol: str, mode: StrategyMode) -> tuple:
    """Get signal from fast hybrid strategy for a symbol using the specified mode."""
    # Use a shared strategy instance (created once per call)
    if not hasattr(_get_hybrid_signal, '_strategy'):
        _get_hybrid_signal._strategy = FastHybridStrategy(mode)
    elif _get_hybrid_signal._strategy.mode != mode:
        _get_hybrid_signal._strategy = FastHybridStrategy(mode)
    
    signal = _get_hybrid_signal._strategy.evaluate(symbol)
    return signal.action, signal.confidence, signal.rationale


@dataclass
class Opportunity:
    symbol: str
    action: str  # BUY or SELL
    conviction_score: float
    technical_score: float
    agreements: list = field(default_factory=list)
    disagreements: list = field(default_factory=list)
    entry_price: float = None
    stop_loss: float = None
    take_profit: float = None
    suggested_size: int = None
    suggested_value: float = None
    blocked: bool = False
    block_reason: str = None
    room_to_target: float = None       # rupees to the nearest opposing swing level, or None if none found
    realistic_reward_risk: float = None  # room_to_target / stop_distance, or None


def _get_latest_signal(table: str, symbol: str) -> dict | None:
    conn = get_connection()
    row = conn.execute(
        f"SELECT action, confidence FROM {table} WHERE symbol = ? ORDER BY timestamp DESC LIMIT 1",
        (symbol,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# Minimum PROMINENCE - how much a peak stands out from its surrounding
# price action, not just whether it's locally higher than nearby candles.
# This is the actual fix for the noise problem order-based local-extrema
# detection can't solve: a wider order still finds noise peaks purely by
# chance on real data (verified during development - even order=15 kept
# finding them), because "locally higher than N neighbors" says nothing
# about how SIGNIFICANT a peak is. Prominence directly measures that.
# The threshold itself is computed per-symbol as 1x ATR (see below) rather
# than a fixed percentage, so it scales with each stock's own volatility.
def _find_room_to_target(symbol: str, action: str, entry_price: float) -> float | None:
    """Distance in rupees to the nearest MEANINGFUL opposing level -
    the nearest sufficiently prominent swing HIGH above entry for a
    BUY (resistance), or swing LOW below entry for a SELL (support).
    None if no such level has been detected in the loaded history."""
    df = load_candles(symbol, limit=200)
    if len(df) < 30:
        return None

    # Prominence threshold from the stock's own volatility, so what counts
    # as a "meaningful" level scales with the stock. A NaN ATR (too few
    # warm-up candles) would make the threshold NaN and find_peaks would
    # return nothing usable or error outright - reporting no room is the
    # honest answer in that case.
    current_atr = compute_atr(df).iloc[-1]
    if pd.isna(current_atr) or current_atr <= 0:
        return None
    prominence = current_atr

    if action == "BUY":
        peak_idx, _ = find_peaks(df["high"].values, prominence=prominence)
        levels_above = [df["high"].iloc[i] for i in peak_idx if df["high"].iloc[i] > entry_price]
        return min(levels_above) - entry_price if levels_above else None
    else:  # SELL - find troughs, i.e. peaks in the INVERTED low series
        trough_idx, _ = find_peaks(-df["low"].values, prominence=prominence)
        levels_below = [df["low"].iloc[i] for i in trough_idx if df["low"].iloc[i] < entry_price]
        return entry_price - max(levels_below) if levels_below else None


def _score_conviction(symbol: str, action: str, technical_score: float) -> tuple:
    """Returns (conviction_score, agreements, disagreements)."""
    points = technical_score
    agreements, disagreements = [], []

    ml_signal = _get_latest_signal("ml_signals", symbol)
    if ml_signal and ml_signal["action"] in ("BUY", "SELL"):
        confidence = ml_signal.get("confidence") or 0.5
        if ml_signal["action"] == action:
            points += ML_WEIGHT * confidence
            agreements.append(f"ML model agrees ({action}, {confidence:.0%} confidence)")
        else:
            points -= ML_WEIGHT * confidence
            disagreements.append(f"ML model disagrees (said {ml_signal['action']}, {confidence:.0%} confidence)")

    llm_signal = _get_latest_signal("llm_signals", symbol)
    if llm_signal and llm_signal["action"] in ("BUY", "SELL"):
        confidence = llm_signal.get("confidence") or 0.5
        if llm_signal["action"] == action:
            points += LLM_WEIGHT * confidence
            agreements.append(f"LLM agent agrees ({action}, {confidence:.0%} confidence)")
        else:
            points -= LLM_WEIGHT * confidence
            disagreements.append(f"LLM agent disagrees (said {llm_signal['action']}, {confidence:.0%} confidence)")

    sentiment = None
    try:
        from analysis.sentiment import latest_sentiment  # lazy - only latest_sentiment's plain SQL read is needed here, not the LLM scoring path
        sentiment = latest_sentiment(symbol)
    except ImportError:
        pass  # analysis.sentiment unavailable - sentiment simply won't contribute to conviction

    if sentiment is not None and abs(sentiment) >= SENTIMENT_THRESHOLD:
        sentiment_bullish = sentiment > 0
        action_bullish = action == "BUY"
        if sentiment_bullish == action_bullish:
            points += SENTIMENT_WEIGHT * abs(sentiment)
            agreements.append(f"News sentiment agrees ({sentiment:+.2f})")
        else:
            points -= SENTIMENT_WEIGHT * abs(sentiment)
            disagreements.append(f"News sentiment disagrees ({sentiment:+.2f})")

    return round(points, 1), agreements, disagreements


def find_opportunities(capital: float | None = None, n: int = 10, use_mode_cache: bool = True, max_symbols: int = 20) -> dict:
    """Returns {'bullish': [...], 'bearish': [...]}, each a list of
    Opportunity objects.

    Ranking uses HybridStrategy: for each symbol, we determine the best
    decision mode (RULE_ONLY, ML_ONLY, or RULE_ML_COMBINED) via backtest
    comparison, then use that mode's signal. The conviction score combines
    the hybrid strategy's confidence with ML/LLM/sentiment agreement.

    Symbols that already have an OPEN paper position are excluded: a second
    position in the same symbol is blocked in EITHER direction
    (risk/portfolio_risk), so offering one here would be offering a trade
    that cannot be taken. The excluded symbols come back under 'excluded'
    so the caller can say why the list is shorter instead of quietly
    showing fewer rows.

    `capital` defaults to the wallet's unified equity (fixed book + float)
    (risk/wallet.py:available_equity) instead of a hardcoded number,
    so sizing tracks the book as it changes with deposits, withdrawals
    and closed trades.

    `use_mode_cache`: If True, use cached best modes from disk (fast).
    If False, run backtests to find best modes (slow, but more accurate).
    
    `max_symbols`: Maximum number of symbols to process (for speed).

    `fast_mode`: If True (default), uses FastHybridStrategy with precomputed
    caches for ~100x speedup. If False, uses original HybridStrategy."""
    # Get the symbols we'll need from rankings first (to warm up only those)
    if capital is None:
        from risk.wallet import available_equity  # lazy - keeps this module importable without the wallet book
        from storage.db import get_connection
        conn = get_connection()
        try:
            capital = available_equity(conn)
        finally:
            conn.close()
    
    # Get top candidates to know which symbols to warm up
    sized = rank_watchlist_with_sizing(capital, n=n * 2, reserve_prior=True)
    held_symbols = get_open_position_symbols()
    
    # Collect symbols we'll process
    symbols_to_process = []
    for direction, key in (("bullish", "bullish_score"), ("bearish", "bearish_score")):
        for entry in sized[direction]:
            if len(symbols_to_process) >= max_symbols:
                break
            symbol = entry["symbol"]
            if symbol not in held_symbols:
                symbols_to_process.append(symbol)
    
    # Warm up caches only for symbols we'll actually process
    warmup_caches_for_symbols(symbols_to_process)
    
    # Use the already-computed sizing results (no duplicate call)
    results = {"bullish": [], "bearish": []}
    excluded = set()
    processed = 0

    for direction, key in (("bullish", "bullish_score"), ("bearish", "bearish_score")):
        for entry in sized[direction]:
            if processed >= max_symbols:
                break
                
            symbol = entry["symbol"]
            # Held is not an opportunity - it is a position you already have.
            # Collected by name, not just dropped, so a short list reads as
            # "you own some of these" rather than as a quiet market.
            if symbol in held_symbols:
                excluded.add(symbol)
                continue

            # Get best mode for this symbol and evaluate hybrid signal
            best_mode = _get_best_mode(symbol, use_cache=use_mode_cache)
            hybrid_action, hybrid_confidence, hybrid_rationale = _get_hybrid_signal(symbol, best_mode)
            
            # Use hybrid signal if it has a trade action, otherwise fall back to rule-based
            action = hybrid_action if hybrid_action in ("BUY", "SELL") else entry["action"]
            
            # Score conviction using hybrid confidence + ML/LLM/sentiment agreement
            conviction, agreements, disagreements = _score_conviction(symbol, action, entry[key])
            
            # Add hybrid strategy info to agreements
            agreements.insert(0, f"Hybrid({best_mode.value}): {hybrid_action} ({hybrid_confidence:.0%}) - {hybrid_rationale}")

            room = None
            realistic_rr = None
            if entry.get("entry_price") is not None and entry.get("stop_loss") is not None:
                room = _find_room_to_target(symbol, action, entry["entry_price"])
                stop_distance = abs(entry["entry_price"] - entry["stop_loss"])
                if room is not None and stop_distance > 0:
                    realistic_rr = round(room / stop_distance, 2)

            results[direction].append(Opportunity(
                symbol=symbol, action=action, conviction_score=conviction,
                technical_score=entry[key], agreements=agreements, disagreements=disagreements,
                entry_price=entry.get("entry_price"), stop_loss=entry.get("stop_loss"),
                take_profit=entry.get("take_profit"), suggested_size=entry.get("suggested_size"),
                suggested_value=entry.get("suggested_value"), blocked=entry.get("blocked", False),
                block_reason=entry.get("block_reason"),
                room_to_target=round(room, 2) if room is not None else None,
                realistic_reward_risk=realistic_rr,
            ))
            processed += 1

        # Sort by conviction score (which now includes hybrid strategy confidence)
        results[direction].sort(key=lambda o: o.conviction_score, reverse=True)
        results[direction] = results[direction][:n]

    results["excluded"] = sorted(excluded)
    return results


if __name__ == "__main__":
    import sys
    capital = float(sys.argv[1]) if len(sys.argv) > 1 else None  # None = wallet available capital
    opportunities = find_opportunities(capital)
    for direction in ("bullish", "bearish"):
        print(f"--- Top {direction} ---")
        for o in opportunities[direction][:3]:
            print(f"  {o.symbol}: conviction={o.conviction_score} (technical={o.technical_score}), "
                  f"agreements={o.agreements}, disagreements={o.disagreements}")
            print(f"    entry={o.entry_price}, stop={o.stop_loss}, target={o.take_profit}, "
                  f"room_to_target={o.room_to_target}, realistic_RR={o.realistic_reward_risk}")