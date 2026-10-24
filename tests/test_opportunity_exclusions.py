"""A held symbol must not be offered as an opportunity.

The filter is one word (`in` vs `not in`) and has already shipped inverted
once, so it is pinned here rather than only in a manual replay.
"""
import trading_agent.analysis.opportunity_finder as finder
import trading_agent.analysis.rankings as rankings
import trading_agent.storage.db as db_module


def _entry(symbol: str, action: str, score: float) -> dict:
    return {"symbol": symbol, "action": action, "bullish_score": score, "bearish_score": score,
            "entry_price": 100.0, "stop_loss": 95.0, "take_profit": 110.0,
            "suggested_size": 10, "suggested_value": 1000.0, "blocked": False, "block_reason": None}


def test_held_symbols_are_excluded_from_both_lists_and_reported(monkeypatch):
    # Mock the ranking to return HELD and FREE symbols
    monkeypatch.setattr(finder, "rank_watchlist_with_sizing", lambda capital, n, reserve_prior=False: {
        "bullish": [_entry("HELD", "BUY", 9.0), _entry("FREE", "BUY", 5.0)],
        "bearish": [_entry("HELD", "SELL", 8.0), _entry("OTHER", "SELL", 4.0)],
    })
    # Patch the function in opportunity_finder where it's defined
    monkeypatch.setattr(finder, "get_open_position_symbols", lambda: {"HELD"})
    monkeypatch.setattr(finder, "_score_conviction", lambda symbol, action, score: (score, [], []))
    # Return BUY for bullish, SELL for bearish from hybrid signal
    monkeypatch.setattr(finder, "_get_best_mode", lambda symbol, candle_limit=5000, use_cache=True, use_fixed_mode=True: finder.StrategyMode.RULE_ML_COMBINED)
    monkeypatch.setattr(finder, "_get_hybrid_signal", lambda symbol, mode: ("BUY" if "HELD" not in symbol and "OTHER" not in symbol else "SELL", 0.8, "test", None, 100.0))

    result = finder.find_opportunities(100000, n=5)

    assert [o.symbol for o in result["bullish"]] == ["FREE"]
    assert [o.symbol for o in result["bearish"]] == ["OTHER"]
    # Reported once, not once per direction it was dropped from.
    assert result["excluded"] == ["HELD"]


def test_nothing_is_excluded_when_no_position_is_held(monkeypatch):
    # Clear cache first
    finder._opportunity_cache.clear()
    
    monkeypatch.setattr(finder, "rank_watchlist_with_sizing", lambda capital, n, reserve_prior=False: {
        "bullish": [_entry("FREE", "BUY", 5.0)],
        "bearish": [],
    })
    monkeypatch.setattr(finder, "get_open_position_symbols", lambda: set())
    monkeypatch.setattr(finder, "_score_conviction", lambda symbol, action, score: (score, [], []))
    monkeypatch.setattr(finder, "_get_best_mode", lambda symbol, candle_limit=5000, use_cache=True, use_fixed_mode=True: finder.StrategyMode.RULE_ML_COMBINED)
    monkeypatch.setattr(finder, "_get_hybrid_signal", lambda symbol, mode: ("BUY", 0.8, "test", None, 100.0))

    result = finder.find_opportunities(100000, n=5)

    assert [o.symbol for o in result["bullish"]] == ["FREE"]
    assert result["excluded"] == []


def test_opportunity_finder_opts_into_reserve_prior(monkeypatch):
    """The opportunity list must be sized as a list, not row by row - it is
    the one surface that answers "what should I buy with this capital"."""
    seen = {}

    def fake_rank(capital, n, reserve_prior=False):
        seen["reserve_prior"] = reserve_prior
        return {"bullish": [], "bearish": []}

    # Clear cache first
    finder._opportunity_cache.clear()
    
    monkeypatch.setattr(finder, "rank_watchlist_with_sizing", fake_rank)
    monkeypatch.setattr(finder, "get_open_position_symbols", lambda: set())
    monkeypatch.setattr(finder, "_get_best_mode", lambda symbol, candle_limit=5000, use_cache=True, use_fixed_mode=True: finder.StrategyMode.RULE_ML_COMBINED)
    monkeypatch.setattr(finder, "_get_hybrid_signal", lambda symbol, mode: ("BUY", 0.8, "test", None, 100.0))

    finder.find_opportunities(100000, n=5)

    assert seen["reserve_prior"] is True
