"""
Portfolio-level risk, on top of position_sizing.py's per-trade math.

The gap this fills: position_sizing.py sizes each trade as if it were
the only position you'd ever hold. If 6 of your BUY signals are all
correlated (same sector, same macro driver, whatever the actual reason),
sizing each at "1% risk" independently still leaves you effectively
making one concentrated bet worth 6% - the per-trade math never sees
that concentration.

Four protections, computed from REAL correlation on your own
historical candles (not an assumed/external sector label, which can be
wrong or missing) and REAL currently-open paper positions (not just
the trade being sized right now):

1. TOTAL RISK BUDGET - caps how much total capital can be at risk
   across ALL open positions simultaneously, not just per-trade.
2. TOTAL EXPOSURE - caps combined NOTIONAL (position VALUE) across all
   open positions. The risk budget alone does not bound this: five quiet
   names each risk almost nothing and still commit the whole book.
3. SAME-SYMBOL BLOCK - a symbol with an open position gets no second one
   in either direction, so the position monitor's single exit
   recommendation stays unambiguous.
4. CORRELATION CLUSTER CAP - caps combined exposure to DIFFERENT
   positions that move together (measured directly, |correlation|
   above a threshold), even if the individual per-trade risk was fine
   on its own.

This module only calculates numbers and reads/writes the PAPER
open_positions ledger - it never places real orders.
"""
from dataclasses import dataclass
from datetime import datetime
import pandas as pd
from core.config import get_config
from core.position_sizing import calculate_position, PositionPlan
from storage.db import get_connection


def _get_risk_config():
    return get_config().risk


@dataclass
class PortfolioAdjustedPlan:
    base_plan: PositionPlan | None
    approved_size: int
    approved_value: float
    blocked: bool
    block_reason: str | None
    total_risk_used_pct: float       # risk already committed by OPEN positions, before this trade
    cluster_exposure_pct: float      # exposure of the correlated cluster this symbol belongs to, before this trade
    correlated_with: list            # open position symbols this one is correlated with
    total_exposure_pct: float        # NOTIONAL already committed (open positions + earlier candidates), before this trade


def get_open_positions() -> list:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM open_positions WHERE status = 'open'").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_open_position(plan: PositionPlan) -> None:
    conn = get_connection()
    conn.execute(
        "INSERT INTO open_positions (symbol, action, entry_price, position_size, position_value, "
        "risk_amount, stop_loss, take_profit, opened_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')",
        (plan.symbol, plan.action, plan.entry_price, plan.position_size, plan.position_value,
         plan.risk_amount, plan.stop_loss, plan.take_profit, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def close_position(position_id: int) -> None:
    conn = get_connection()
    conn.execute("UPDATE open_positions SET status = 'closed' WHERE id = ?", (position_id,))
    conn.commit()
    conn.close()


def compute_correlation_matrix(symbols: list, lookback: int = None) -> pd.DataFrame:
    """Pairwise correlation of RETURNS (not raw price) across symbols,
    computed from actual historical candles - this is what genuinely
    measures co-movement, unlike an assumed sector label."""
    config = _get_risk_config()
    if lookback is None:
        lookback = config.correlation_lookback
    conn = get_connection()
    returns = {}
    for symbol in symbols:
        df = pd.read_sql_query(
            "SELECT timestamp, close FROM candles WHERE symbol = ? ORDER BY timestamp DESC LIMIT ?",
            conn, params=(symbol, lookback),
        )
        if len(df) < 20:
            continue
        df = df.iloc[::-1].reset_index(drop=True)
        returns[symbol] = df.set_index("timestamp")["close"].pct_change()
    conn.close()

    if len(returns) < 2:
        return pd.DataFrame()
    returns_df = pd.DataFrame(returns)
    return returns_df.corr()


def _correlated_symbols(symbol: str, other_symbols: list, corr_matrix: pd.DataFrame) -> list:
    config = _get_risk_config()
    if symbol not in corr_matrix.columns:
        return []
    correlated = []
    for other in other_symbols:
        if other == symbol or other not in corr_matrix.columns:
            continue
        corr_value = corr_matrix.loc[symbol, other]
        if pd.notna(corr_value) and abs(corr_value) >= config.correlation_threshold:
            correlated.append(other)
    return correlated


def calculate_portfolio_adjusted_position(
    symbol: str,
    action: str,
    capital: float,
    reserved_risk_amount: float = 0.0,
    reserved_value: float = 0.0,
) -> PortfolioAdjustedPlan:
    """Size one candidate against the open book's portfolio limits.

    reserved_risk_amount / reserved_value are risk and NOTIONAL already
    promised by EARLIER candidates in the same list. Callers that want a list
    to be self-consistent pass them as they go (analysis/rankings.py's
    reserve_prior); everything else leaves them at 0 and gets the historical
    "compare this row against the open book only" behaviour.
    """
    base_plan = calculate_position(symbol, action, capital)
    if base_plan is None:
        return PortfolioAdjustedPlan(
            base_plan=None, approved_size=0, approved_value=0.0, blocked=True,
            block_reason="Could not calculate a base position (not enough data, or action is HOLD).",
            total_risk_used_pct=0.0, cluster_exposure_pct=0.0, correlated_with=[],
            total_exposure_pct=0.0,
        )

    config = _get_risk_config()
    open_positions = get_open_positions()
    total_risk_used = sum(p["risk_amount"] for p in open_positions) + reserved_risk_amount
    total_risk_used_pct = (total_risk_used / capital) * 100 if capital > 0 else 0.0
    remaining_budget_pct = config.total_risk_budget_pct - total_risk_used_pct

    # Total NOTIONAL already committed: open positions' recorded value, plus
    # whatever earlier candidates in this list have already been promised.
    committed_value = sum(p["position_value"] for p in open_positions) + reserved_value
    total_exposure_pct = (committed_value / capital) * 100 if capital > 0 else 0.0

    # --- Check 0: same symbol already has an open position (hard block) ---
    # A symbol with an active paper position cannot receive a second
    # position in EITHER direction: pyramiding (adding to the same side)
    # or hedging (opening the opposite side) would leave the position
    # monitor's single exit recommendation ambiguous, and "close the loser
    # first" is not a rule the code can enforce on your behalf. Close the
    # existing position before opening a new one in that symbol.
    for p in open_positions:
        if p["symbol"] == symbol:
            return PortfolioAdjustedPlan(
                base_plan=base_plan, approved_size=0, approved_value=0.0, blocked=True,
                block_reason=f"Open position already in {symbol} ({p['action']} {p['position_size']} shares) - "
                             f"close it before opening a new position in this symbol.",
                total_risk_used_pct=total_risk_used_pct, cluster_exposure_pct=0.0, correlated_with=[],
                total_exposure_pct=total_exposure_pct,
            )

    # --- Check 1: total risk budget ---
    if remaining_budget_pct <= 0:
        return PortfolioAdjustedPlan(
            base_plan=base_plan, approved_size=0, approved_value=0.0, blocked=True,
            block_reason=f"Total risk budget ({config.total_risk_budget_pct}%) already committed by open positions "
                         f"({total_risk_used_pct:.1f}% used) - no room for a new trade.",
            total_risk_used_pct=total_risk_used_pct, cluster_exposure_pct=0.0, correlated_with=[],
            total_exposure_pct=total_exposure_pct,
        )

    # --- Check 2: total notional exposure ---
    # The per-trade cap (position_sizing) and Check 1 bound RISK. Neither
    # bounds NOTIONAL: five quiet names each at 20% of capital risk almost
    # nothing individually and still commit the whole book, and a list of
    # independently-approved candidates can promise far more value than the
    # book can hold at once. This is that missing bound - the sum of open
    # position VALUE (plus any earlier candidates this caller reserved) may not
    # cross max_total_exposure_pct, and a new trade is scaled into whatever
    # room is left, or refused outright when there is none.
    remaining_exposure_pct = config.max_total_exposure_pct - total_exposure_pct
    if remaining_exposure_pct <= 0:
        return PortfolioAdjustedPlan(
            base_plan=base_plan, approved_size=0, approved_value=0.0, blocked=True,
            block_reason=f"Total exposure cap ({config.max_total_exposure_pct}%) already committed "
                         f"({total_exposure_pct:.1f}% of capital in open positions) - no room for a new trade.",
            total_risk_used_pct=total_risk_used_pct, cluster_exposure_pct=0.0, correlated_with=[],
            total_exposure_pct=total_exposure_pct,
        )

    # Scale the base plan's size down to whichever limit binds first. The
    # binding limit's NAME is kept, so a size cut to zero can say which cap
    # refused it rather than a vague "risk limits".
    base_plan_risk_pct = (base_plan.risk_amount / capital) * 100 if capital > 0 else 0.0
    size_scale, limiter = 1.0, None
    if base_plan_risk_pct > 0:
        risk_scale = min(1.0, remaining_budget_pct / base_plan_risk_pct)
        if risk_scale < size_scale:
            size_scale, limiter = risk_scale, "total risk budget"

    # ... and into the remaining NOTIONAL room. No "at least one share" floor
    # here: a single share can itself breach a tight cap, and the cap is a hard
    # bound - max_position_pct already guarantees a real size when there is room.
    if base_plan.position_value > 0:
        exposure_scale = min(1.0, capital * (remaining_exposure_pct / 100) / base_plan.position_value)
        if exposure_scale < size_scale:
            size_scale, limiter = exposure_scale, "total exposure cap"

    # Same-symbol concentration is no longer a cap: check 0 above blocks
    # any new position in a symbol that already has one open, so the code
    # below only ever sizes symbols with no existing exposure.

    # --- Check 3: correlation cluster exposure ---
    open_symbols = [p["symbol"] for p in open_positions]
    cluster_exposure_pct, correlated_with = 0.0, []
    if open_symbols:
        all_symbols = list(set(open_symbols + [symbol]))
        corr_matrix = compute_correlation_matrix(all_symbols)
        correlated_with = _correlated_symbols(symbol, open_symbols, corr_matrix)

        if correlated_with:
            cluster_value = sum(p["position_value"] for p in open_positions if p["symbol"] in correlated_with)
            cluster_exposure_pct = (cluster_value / capital) * 100 if capital > 0 else 0.0
            remaining_cluster_room_pct = config.max_cluster_exposure_pct - cluster_exposure_pct

            if remaining_cluster_room_pct <= 0:
                return PortfolioAdjustedPlan(
                    base_plan=base_plan, approved_size=0, approved_value=0.0, blocked=True,
                    block_reason=f"Correlated cluster ({', '.join(correlated_with)}) already at "
                                 f"{cluster_exposure_pct:.1f}% of capital (cap {config.max_cluster_exposure_pct}%) - "
                                 f"{symbol} moves with these, no room for more correlated exposure.",
                    total_risk_used_pct=total_risk_used_pct, cluster_exposure_pct=cluster_exposure_pct,
                    correlated_with=correlated_with, total_exposure_pct=total_exposure_pct,
                )

            max_value_by_cluster = capital * (remaining_cluster_room_pct / 100)
            max_size_by_cluster = int(max_value_by_cluster / base_plan.entry_price) if base_plan.entry_price > 0 else 0
            # Ensure at least 1 share if base_plan has shares and we have enough capital for 1 share
            if base_plan.position_size > 0 and max_size_by_cluster == 0 and base_plan.entry_price <= capital:
                max_size_by_cluster = 1
            cluster_scale = min(1.0, max_size_by_cluster / base_plan.position_size) if base_plan.position_size > 0 else 1.0
            if cluster_scale < size_scale:
                size_scale, limiter = cluster_scale, "correlation cluster"

    approved_size = int(base_plan.position_size * size_scale)
    approved_value = round(approved_size * base_plan.entry_price, 2)

    if approved_size > 0:
        block_reason = None
    elif limiter:
        block_reason = f"Scaled to zero by the {limiter}."
    else:
        block_reason = "Base plan already sized to zero shares."

    return PortfolioAdjustedPlan(
        base_plan=base_plan, approved_size=approved_size, approved_value=approved_value,
        blocked=(approved_size == 0), block_reason=block_reason,
        total_risk_used_pct=round(total_risk_used_pct, 2), cluster_exposure_pct=round(cluster_exposure_pct, 2),
        correlated_with=correlated_with, total_exposure_pct=round(total_exposure_pct, 2),
    )


def exposure_summary(capital: float) -> dict:
    """Committed notional, free room and exposure % for the live book.

    Committed is the SAME sum the total-exposure cap enforces - open
    positions' recorded `position_value` - so /api/wallet can never report an
    exposure the sizing gate would disagree with. `free_notional` floors at 0:
    a book already past the cap has no room, not negative room.
    """
    config = _get_risk_config()
    committed = round(sum(float(p["position_value"]) for p in get_open_positions()), 2)
    cap_value = capital * (config.max_total_exposure_pct / 100) if capital > 0 else 0.0
    return {
        "committed_notional": committed,
        "free_notional": round(max(0.0, cap_value - committed), 2),
        "exposure_pct": round((committed / capital) * 100, 2) if capital > 0 else 0.0,
    }


if __name__ == "__main__":
    import sys
    symbol = sys.argv[1] if len(sys.argv) > 1 else "RELIANCE"
    action = sys.argv[2] if len(sys.argv) > 2 else "BUY"
    capital = float(sys.argv[3]) if len(sys.argv) > 3 else 100000.0

    plan = calculate_portfolio_adjusted_position(symbol, action, capital)
    print(f"--- Portfolio-adjusted plan: {symbol} {action} ---")
    print(f"  Base (per-trade only) size: {plan.base_plan.position_size if plan.base_plan else 0}")
    print(f"  Approved size: {plan.approved_size} ({plan.approved_value})")
    print(f"  Blocked: {plan.blocked} ({plan.block_reason})")
    print(f"  Total risk already used: {plan.total_risk_used_pct}%")
    print(f"  Total exposure already committed: {plan.total_exposure_pct}%")
    print(f"  Cluster exposure: {plan.cluster_exposure_pct}% (correlated with: {plan.correlated_with})")
