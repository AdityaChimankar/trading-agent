"""Open (paper) position endpoints: list, open, close."""
import dataclasses

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api.serializers import jsonable

router = APIRouter(prefix="/api", tags=["positions"])


class OpenPositionRequest(BaseModel):
    symbol: str
    action: str = Field(pattern="^(BUY|SELL)$")
    capital: float = Field(gt=0)
    # Optional: use the opportunity's signal timestamp/price for cost tracking
    signal_timestamp: str | None = None
    signal_price: float | None = None
    signal_source: str | None = None  # 'rule' | 'ml' | 'hybrid' | 'llm'


@router.get("/positions")
def list_positions():
    """Every open position with live P&L and a HOLD/SELL indicator."""
    from risk.position_monitor import get_all_position_statuses

    return jsonable(get_all_position_statuses(fetch_current_signals=True))


@router.post("/positions", status_code=201)
def open_position(payload: OpenPositionRequest):
    """Records a paper position at the PORTFOLIO-ADJUSTED size.

    The action should match the opportunity's hybrid action (which may be from
    ML, rule, or combined mode). This ensures the position is sized according
    to the same signal that generated the opportunity.
    """
    from risk.portfolio_risk import add_open_position, calculate_portfolio_adjusted_position

    adjusted = calculate_portfolio_adjusted_position(payload.symbol, payload.action, payload.capital)

    if adjusted.blocked or adjusted.base_plan is None or adjusted.approved_size <= 0:
        raise HTTPException(
            status_code=400,
            detail=adjusted.block_reason or "Position blocked by portfolio risk limits.",
        )

    recorded_plan = dataclasses.replace(
        adjusted.base_plan,
        position_size=adjusted.approved_size,
        position_value=adjusted.approved_value,
    )
    
    # Use signal timestamp/price from payload if provided, otherwise look up from opportunities
    signal_timestamp = payload.signal_timestamp
    signal_price = payload.signal_price
    signal_source = payload.signal_source
    
    if signal_timestamp is None or signal_price is None or signal_source is None:
        try:
            from analysis.opportunity_finder import find_opportunities
            opportunities = find_opportunities(capital=payload.capital, n=50)
            for direction in ("bullish", "bearish"):
                for opp in opportunities[direction]:
                    if opp.symbol == payload.symbol and opp.action == payload.action:
                        signal_timestamp = signal_timestamp or opp.signal_timestamp
                        signal_price = signal_price or opp.signal_price
                        # Determine signal source from the opportunity's hybrid mode
                        if signal_source is None and hasattr(opp, 'signal_source'):
                            signal_source = opp.signal_source
                        break
        except Exception:
            pass  # Don't fail if opportunity lookup fails
    
    # Default to 'rule' if not determined
    if signal_source is None:
        signal_source = 'rule'
    
    add_open_position(recorded_plan, signal_timestamp=signal_timestamp, signal_price=signal_price, signal_source=signal_source)
    return jsonable({"symbol": payload.symbol, "action": payload.action,
                     "shares": adjusted.approved_size, "value": adjusted.approved_value})


@router.post("/positions/{position_id}/close")
def close(position_id: int):
    from risk.wallet import _SLIPPAGE, record_trade_close
    from storage.db import get_connection

    conn = get_connection()
    try:
        # The position is read BEFORE anything is written: record_trade_close()
        # marks it closed and inserts the trade row in one transaction, so a
        # close can never land in the wallet book without the position being
        # recorded as closed, or the other way round.
        row = _open_positions_for_close(conn, position_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Position not found.")
        position = dict(row)

        result = record_trade_close(
            conn, position,
            exit_reason="manually_closed",
            slippage_deducted=round(float(position["position_value"]) * _SLIPPAGE(), 2),
        )
        return {"closed": position_id, "symbol": position["symbol"],
                "realized_pnl_rupees": result["realized_pnl_rupees"],
                "exit_price": result["exit_price"]}
    finally:
        conn.close()


def _open_positions_for_close(conn, position_id: int):
    return conn.execute(
        "SELECT * FROM open_positions WHERE id = ? AND status = 'open'",
        (position_id,),
    ).fetchone()
