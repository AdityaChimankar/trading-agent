"""Watchlist + watchlist-wide ranking endpoints."""
from fastapi import APIRouter, Query

from api.serializers import jsonable
from db import get_watchlist_symbols

router = APIRouter(prefix="/api", tags=["watchlist"])


@router.get("/watchlist")
def watchlist():
    """Symbols available in the UI's symbol selector."""
    return {"symbols": get_watchlist_symbols()}


@router.get("/rankings")
def rankings(capital: float = Query(100000.0, gt=0)):
    """Sidebar data: technical scores for the whole watchlist (with
    bias-vs-live-P&L contradictions annotated) plus a top-10 sized
    suggestion list for each direction.

    `scored` and the sized lists come from two independent
    rank_watchlist() calls, exactly as the Streamlit sidebar did - the
    front end joins them by symbol to attach the contradiction flag to
    each sized entry.
    """
    from rankings import (
        annotate_contradictions,
        get_contradictions,
        rank_watchlist,
        rank_watchlist_with_sizing,
    )
    from position_monitor import get_all_position_statuses

    statuses = get_all_position_statuses(fetch_current_signals=True)
    scored = annotate_contradictions(rank_watchlist(), statuses)
    sized = rank_watchlist_with_sizing(capital, n=10)

    return jsonable({
        "scored": scored,
        "bullish": sized["bullish"],
        "bearish": sized["bearish"],
        "contradictions": get_contradictions(scored),
    })
