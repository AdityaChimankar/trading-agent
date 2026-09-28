"""Top-opportunity endpoint (conviction-ranked bullish/bearish picks)."""
from fastapi import APIRouter, Query

from api.serializers import jsonable

router = APIRouter(prefix="/api", tags=["opportunities"])


@router.get("/opportunities")
def opportunities(capital: float = Query(100000.0, gt=0), n: int = Query(10, ge=1, le=50)):
    from analysis.opportunity_finder import find_opportunities

    result = find_opportunities(capital, n=n)
    return jsonable({"bullish": result["bullish"], "bearish": result["bearish"]})
