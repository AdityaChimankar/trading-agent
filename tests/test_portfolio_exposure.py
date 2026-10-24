"""Total NOTIONAL exposure cap and reserved-against-prior sizing.

The defect this pins: risk was capped per trade, exposure was not. Five quiet
names each at 20% of capital risk almost nothing individually and still commit
the whole book, and a list of independently-approved candidates summed to
several times the book's value. These tests cover the bound itself, the
self-consistent list mode, and the wallet split that reports it.
"""
import types

import pandas as pd
import pytest

import trading_agent.analysis.rankings as rankings
import trading_agent.risk.portfolio_risk as pr
from trading_agent.core.position_sizing import PositionSizeResult


def _plan(symbol="X", action="BUY", entry=100.0, size=100, stop_distance=5.0):
    return PositionSizeResult(
        shares=size,
        position_value=size * entry,
        risk_amount=size * stop_distance,
        stop_price=entry - stop_distance if action == "BUY" else entry + stop_distance,
        take_profit_price=None,
        atr_value=stop_distance / 1.5,
        atr_percentile=0.5,
        risk_pct_of_portfolio=0.0,
    )


def _open(symbol, value, risk=0.0):
    return {"symbol": symbol, "action": "BUY", "entry_price": 100.0,
            "position_size": int(value / 100), "position_value": float(value),
            "risk_amount": float(risk)}


def _scored(n):
    return [{"symbol": f"S{i}", "bullish_score": float(10 - i), "bearish_score": 0.0}
            for i in range(n)]


@pytest.fixture
def book(monkeypatch):
    """A 50%-of-100000 exposure cap on a book with no correlation clusters."""
    settings = types.SimpleNamespace(
        total_risk_budget_pct=6.0,
        max_cluster_exposure_pct=20.0,
        max_total_exposure_pct=50.0,
    )
    monkeypatch.setattr(pr, "_get_risk_config", lambda: settings)
    monkeypatch.setattr(pr, "compute_correlation_matrix",
                        lambda symbols, lookback=None: pd.DataFrame())
    return settings


# --- the bound -----------------------------------------------------------------

def test_open_book_with_room_leaves_sizes_unchanged(book, monkeypatch):
    monkeypatch.setattr(pr, "get_open_positions", lambda: [])
    monkeypatch.setattr(pr, "calculate_position", lambda s, a, c: _plan(s, a))

    plan = pr.calculate_portfolio_adjusted_position("X", "BUY", 100000)

    assert plan.approved_size == 100
    assert plan.approved_value == 10000.0
    assert plan.blocked is False
    assert plan.total_exposure_pct == 0.0


def test_open_exposure_scales_candidate_into_remaining_room(book, monkeypatch):
    monkeypatch.setattr(pr, "get_open_positions", lambda: [_open("A", 45000, risk=500)])
    monkeypatch.setattr(pr, "calculate_position", lambda s, a, c: _plan(s, a))

    plan = pr.calculate_portfolio_adjusted_position("X", "BUY", 100000)

    assert plan.total_exposure_pct == 45.0
    assert plan.approved_size == 50          # 10% of 100000 left, 100 rupees/share
    assert plan.approved_value == 5000.0


def test_exposure_cap_blocks_even_when_the_risk_budget_has_room(book, monkeypatch):
    """The exact hole: risk used is trivial (0.5%), but notional is already at
    the cap. Only the exposure bound catches this."""
    monkeypatch.setattr(pr, "get_open_positions", lambda: [_open("A", 50000, risk=500)])
    monkeypatch.setattr(pr, "calculate_position", lambda s, a, c: _plan(s, a))

    plan = pr.calculate_portfolio_adjusted_position("X", "BUY", 100000)

    assert plan.total_risk_used_pct < 6.0
    assert plan.blocked is True
    assert plan.approved_size == 0
    assert "exposure" in plan.block_reason.lower()
    assert "50" in plan.block_reason  # the cap value is named


def test_reserved_value_from_earlier_candidates_consumes_room(book, monkeypatch):
    monkeypatch.setattr(pr, "get_open_positions", lambda: [])
    monkeypatch.setattr(pr, "calculate_position", lambda s, a, c: _plan(s, a))

    plan = pr.calculate_portfolio_adjusted_position(
        "X", "BUY", 100000, reserved_value=48000.0)

    assert plan.total_exposure_pct == 48.0
    assert plan.approved_size == 20          # only 2000 rupees of room left


def test_reserved_risk_from_earlier_candidates_consumes_the_budget(book, monkeypatch):
    monkeypatch.setattr(pr, "get_open_positions", lambda: [])
    monkeypatch.setattr(pr, "calculate_position", lambda s, a, c: _plan(s, a))

    plan = pr.calculate_portfolio_adjusted_position(
        "X", "BUY", 100000, reserved_risk_amount=5900.0)

    # 6% budget - 5.9% already reserved = 0.1% (100 rupees) of room; 5 per share.
    assert plan.approved_size == 20


def test_zero_capital_does_not_divide_by_zero(book, monkeypatch):
    monkeypatch.setattr(pr, "get_open_positions", lambda: [])
    monkeypatch.setattr(pr, "calculate_position", lambda s, a, c: _plan(s, a))

    plan = pr.calculate_portfolio_adjusted_position("X", "BUY", 0)

    assert plan.total_exposure_pct == 0.0
    assert plan.approved_size == 0            # no room, and no crash


# --- the self-consistent list mode ---------------------------------------------

def test_reserve_prior_bounds_the_whole_list_to_the_cap(book, monkeypatch):
    monkeypatch.setattr(rankings, "rank_watchlist", lambda max_symbols=500: _scored(3))
    monkeypatch.setattr(pr, "get_open_positions", lambda: [])
    monkeypatch.setattr(pr, "calculate_position",
                        lambda s, a, c: _plan(s, a, size=400, entry=100.0))

    sized = rankings.rank_watchlist_with_sizing(100000, n=3, reserve_prior=True)
    values = [e["suggested_value"] for e in sized["bullish"]]

    assert values[0] == 40000.0               # first fits whole: cap is 50%
    assert values[1] == 10000.0               # second gets only the leftover 10%
    assert values[2] == 0.0                   # third has no room
    assert sum(values) <= 50000.0
    assert sized["bullish"][2]["blocked"] is True
    assert "exposure" in sized["bullish"][2]["block_reason"].lower()


def test_blocked_row_releases_no_reservation(book, monkeypatch):
    """A row refused by Check 0 (symbol already open) promised nothing, so it
    must not shrink the rows after it."""
    monkeypatch.setattr(rankings, "rank_watchlist", lambda max_symbols=500: _scored(2))
    monkeypatch.setattr(pr, "get_open_positions", lambda: [_open("S0", 10000)])
    monkeypatch.setattr(pr, "calculate_position",
                        lambda s, a, c: _plan(s, a, size=400, entry=100.0))

    sized = rankings.rank_watchlist_with_sizing(100000, n=2, reserve_prior=True)
    rows = sized["bullish"]

    assert rows[0]["symbol"] == "S0" and rows[0]["blocked"] is True
    assert rows[1]["suggested_value"] == 40000.0   # full size: S0 reserved nothing


def test_default_sizing_stays_per_row_and_unchanged(book, monkeypatch):
    """Callers that do not opt in get exactly the historical behaviour: every
    row sized independently against what is genuinely open."""
    monkeypatch.setattr(rankings, "rank_watchlist", lambda max_symbols=500: _scored(3))
    monkeypatch.setattr(pr, "get_open_positions", lambda: [])
    monkeypatch.setattr(pr, "calculate_position",
                        lambda s, a, c: _plan(s, a, size=400, entry=100.0))

    sized = rankings.rank_watchlist_with_sizing(100000, n=3)

    assert [e["suggested_value"] for e in sized["bullish"]] == [40000.0] * 3
    assert [e["blocked"] for e in sized["bullish"]] == [False, False, False]


# --- the wallet split ----------------------------------------------------------

def test_exposure_summary_reports_committed_free_and_pct(book, monkeypatch):
    monkeypatch.setattr(pr, "get_open_positions", lambda: [_open("A", 45000), _open("B", 5000)])

    assert pr.exposure_summary(100000) == {
        "committed_notional": 50000.0, "free_notional": 0.0, "exposure_pct": 50.0,
    }


def test_free_notional_floors_at_zero_when_over_cap(book, monkeypatch):
    monkeypatch.setattr(pr, "get_open_positions", lambda: [_open("A", 80000)])

    summary = pr.exposure_summary(100000)

    assert summary["committed_notional"] == 80000.0
    assert summary["free_notional"] == 0.0    # no negative room
    assert summary["exposure_pct"] == 80.0


def test_exposure_summary_handles_zero_capital(book, monkeypatch):
    monkeypatch.setattr(pr, "get_open_positions", lambda: [_open("A", 1000)])

    assert pr.exposure_summary(0) == {
        "committed_notional": 1000.0, "free_notional": 0.0, "exposure_pct": 0.0,
    }


def test_sizing_uses_real_price_and_cannot_undo_exposure_cap(book, monkeypatch):
    # Both constraints apply: exposure allows 20 shares, risk allows 50.
    monkeypatch.setattr(pr, "get_open_positions", lambda: [_open("A", 45000, risk=5750)])
    monkeypatch.setattr(pr, "calculate_position", lambda s, a, c: _plan(s, a, entry=250, size=100))
    plan = pr.calculate_portfolio_adjusted_position("X", "BUY", 100000)
    assert plan.entry_price == 250
    assert plan.approved_size == 20
    assert plan.approved_value == 5000


def test_duplicate_symbol_is_blocked(book, monkeypatch):
    monkeypatch.setattr(pr, "get_open_positions", lambda: [_open("X", 1000)])
    monkeypatch.setattr(pr, "calculate_position", lambda s, a, c: _plan(s, a))
    plan = pr.calculate_portfolio_adjusted_position("X", "BUY", 100000)
    assert plan.blocked and plan.approved_size == 0
