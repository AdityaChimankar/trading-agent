"""Top-opportunity endpoint (rule-score-ranked bullish/bearish picks).

Capital is optional: when omitted it defaults to the wallet's available
paper balance (risk/wallet.py:available_capital), so the panel tracks the
book instead of a number typed into a settings field. The effective
`available_capital` is echoed back so the front end can display it.
Symbols with open positions are excluded by the finder (and re-checked at
sizing time by portfolio_risk), and echoed back under `excluded` so the UI
can explain why the list is shorter than the requested `n`.
"""
from fastapi import APIRouter, Query

from api.serializers import jsonable

router = APIRouter(prefix="/api", tags=["opportunities"])


@router.get("/opportunities")
def opportunities(capital: float | None = Query(None, gt=0), n: int = Query(10, ge=1, le=50)):
    from analysis.opportunity_finder import find_opportunities
    from risk.wallet import available_capital
    from storage.db import get_connection

    conn = get_connection()
    try:
        effective_capital = capital if capital is not None else available_capital(conn)
    finally:
        conn.close()

    result = find_opportunities(effective_capital, n=n)
    return jsonable({
        "bullish": result["bullish"],
        "bearish": result["bearish"],
        "excluded": result["excluded"],
        "available_capital": round(effective_capital, 2),
    })
