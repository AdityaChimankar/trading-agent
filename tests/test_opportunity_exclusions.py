"""A held symbol must not be offered as an opportunity.

The filter is one word (`in` vs `not in`) and has already shipped inverted
once, so it is pinned here rather than only in a manual replay.
"""
import analysis.opportunity_finder as finder


def _entry(symbol: str, action: str, score: float) -> dict:
    return {"symbol": symbol, "action": action, "bullish_score": score, "bearish_score": score}


def test_held_symbols_are_excluded_from_both_lists_and_reported(monkeypatch):
    monkeypatch.setattr(finder, "rank_watchlist_with_sizing", lambda capital, n, reserve_prior=False: {
        "bullish": [_entry("HELD", "BUY", 9.0), _entry("FREE", "BUY", 5.0)],
        "bearish": [_entry("HELD", "SELL", 8.0), _entry("OTHER", "SELL", 4.0)],
    })
    monkeypatch.setattr(finder, "get_open_position_symbols", lambda: {"HELD"})
    monkeypatch.setattr(finder, "_score_conviction", lambda symbol, action, score: (score, [], []))

    result = finder.find_opportunities(100000, n=5)

    assert [o.symbol for o in result["bullish"]] == ["FREE"]
    assert [o.symbol for o in result["bearish"]] == ["OTHER"]
    # Reported once, not once per direction it was dropped from.
    assert result["excluded"] == ["HELD"]


def test_nothing_is_excluded_when_no_position_is_held(monkeypatch):
    monkeypatch.setattr(finder, "rank_watchlist_with_sizing", lambda capital, n, reserve_prior=False: {
        "bullish": [_entry("FREE", "BUY", 5.0)],
        "bearish": [],
    })
    monkeypatch.setattr(finder, "get_open_position_symbols", lambda: set())
    monkeypatch.setattr(finder, "_score_conviction", lambda symbol, action, score: (score, [], []))

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

    monkeypatch.setattr(finder, "rank_watchlist_with_sizing", fake_rank)
    monkeypatch.setattr(finder, "get_open_position_symbols", lambda: set())

    finder.find_opportunities(100000, n=5)

    assert seen["reserve_prior"] is True
