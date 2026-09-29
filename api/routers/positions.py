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


@router.get("/positions")
def list_positions():
    """Every open position with live P&L and a HOLD/SELL indicator."""
    from risk.position_monitor import get_all_position_statuses

    return jsonable(get_all_position_statuses(fetch_current_signals=True))


@router.post("/positions", status_code=201)
def open_position(payload: OpenPositionRequest):
    """Records a paper position at the PORTFOLIO-ADJUSTED size.

    Note: the old Streamlit panel labelled its button with
    `approved_size` but actually recorded `base_plan` - the per-trade
    size BEFORE the correlation / cluster / risk-budget reductions were
    applied. So a reduced trade was silently recorded larger than the
    UI claimed. This records the approved size, which is what the label
    always promised.
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
    add_open_position(recorded_plan)
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
