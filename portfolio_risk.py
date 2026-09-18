"""
Portfolio-level risk, on top of position_sizing.py's per-trade math.

The gap this fills: position_sizing.py sizes each trade as if it were
the only position you'd ever hold. If 6 of your BUY signals are all
correlated (same sector, same macro driver, whatever the actual reason),
sizing each at "1% risk" independently still leaves you effectively
making one concentrated bet worth 6% - the per-trade math never sees
that concentration.

Three protections, computed from REAL correlation on your own
historical candles (not an assumed/external sector label, which can be
wrong or missing) and REAL currently-open paper positions (not just
the trade being sized right now):

1. TOTAL RISK BUDGET - caps how much total capital can be at risk
   across ALL open positions simultaneously, not just per-trade.
2. SAME-SYMBOL CONCENTRATION - caps cumulative exposure to the SAME
   stock across multiple open positions, since each individual sizing
   call otherwise has no memory of what's already open in that symbol.
3. CORRELATION CLUSTER CAP - caps combined exposure to DIFFERENT
   positions that move together (measured directly, |correlation|
   above a threshold), even if the individual per-trade risk was fine
   on its own.

This module only calculates numbers and reads/writes the PAPER
open_positions ledger - it never places real orders.
"""
from dataclasses import dataclass
from datetime import datetime
import pandas as pd
from db import get_connection
from position_sizing import calculate_position, PositionPlan, MAX_POSITION_PCT_OF_CAPITAL

TOTAL_RISK_BUDGET_PCT = 6.0       # max % of capital at risk across ALL open positions at once
CORRELATION_LOOKBACK = 500        # candles used to compute correlation
CORRELATION_THRESHOLD = 0.6       # |correlation| above this = "same cluster"
MAX_CLUSTER_EXPOSURE_PCT = 20.0   # max % of capital in one correlated cluster (open + proposed combined)


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


def compute_correlation_matrix(symbols: list, lookback: int = CORRELATION_LOOKBACK) -> pd.DataFrame:
    """Pairwise correlation of RETURNS (not raw price) across symbols,
    computed from actual historical candles - this is what genuinely
    measures co-movement, unlike an assumed sector label."""
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
    if symbol not in corr_matrix.columns:
        return []
    correlated = []
    for other in other_symbols:
        if other == symbol or other not in corr_matrix.columns:
            continue
        corr_value = corr_matrix.loc[symbol, other]
        if pd.notna(corr_value) and abs(corr_value) >= CORRELATION_THRESHOLD:
            correlated.append(other)
    return correlated


def calculate_portfolio_adjusted_position(symbol: str, action: str, capital: float) -> PortfolioAdjustedPlan:
    base_plan = calculate_position(symbol, action, capital)
    if base_plan is None:
        return PortfolioAdjustedPlan(
            base_plan=None, approved_size=0, approved_value=0.0, blocked=True,
            block_reason="Could not calculate a base position (not enough data, or action is HOLD).",
            total_risk_used_pct=0.0, cluster_exposure_pct=0.0, correlated_with=[],
        )

    open_positions = get_open_positions()
    total_risk_used = sum(p["risk_amount"] for p in open_positions)
    total_risk_used_pct = (total_risk_used / capital) * 100 if capital > 0 else 0.0
    remaining_budget_pct = TOTAL_RISK_BUDGET_PCT - total_risk_used_pct

    # --- Check 1: total risk budget ---
    if remaining_budget_pct <= 0:
        return PortfolioAdjustedPlan(
            base_plan=base_plan, approved_size=0, approved_value=0.0, blocked=True,
            block_reason=f"Total risk budget ({TOTAL_RISK_BUDGET_PCT}%) already committed by open positions "
                         f"({total_risk_used_pct:.1f}% used) - no room for a new trade.",
            total_risk_used_pct=total_risk_used_pct, cluster_exposure_pct=0.0, correlated_with=[],
        )

    # Scale the base plan's size down if its own risk would exceed the REMAINING budget
    # (even before considering correlation)
    base_plan_risk_pct = (base_plan.risk_amount / capital) * 100 if capital > 0 else 0.0
    size_scale = min(1.0, remaining_budget_pct / base_plan_risk_pct) if base_plan_risk_pct > 0 else 1.0

    # --- Check 2: same-symbol concentration ---
    # The correlation check below correctly EXCLUDES the symbol itself
    # (correlation-with-self is meaningless), but that means it can
    # never catch accumulating multiple positions in the SAME stock -
    # each call to position_sizing.calculate_position() independently
    # allows up to MAX_POSITION_PCT_OF_CAPITAL with no memory of
    # exposure already open in that same symbol. This check closes
    # that gap directly.
    existing_same_symbol_value = sum(p["position_value"] for p in open_positions if p["symbol"] == symbol)
    existing_same_symbol_pct = (existing_same_symbol_value / capital) * 100 if capital > 0 else 0.0
    remaining_same_symbol_pct = MAX_POSITION_PCT_OF_CAPITAL - existing_same_symbol_pct

    if remaining_same_symbol_pct <= 0:
        return PortfolioAdjustedPlan(
            base_plan=base_plan, approved_size=0, approved_value=0.0, blocked=True,
            block_reason=f"Already have {existing_same_symbol_pct:.1f}% of capital open in {symbol} "
                         f"(single-symbol cap {MAX_POSITION_PCT_OF_CAPITAL}%) - no room to add more.",
            total_risk_used_pct=total_risk_used_pct, cluster_exposure_pct=0.0, correlated_with=[],
        )

    max_value_by_same_symbol = capital * (remaining_same_symbol_pct / 100)
    max_size_by_same_symbol = int(max_value_by_same_symbol / base_plan.entry_price) if base_plan.entry_price > 0 else 0
    same_symbol_scale = min(1.0, max_size_by_same_symbol / base_plan.position_size) if base_plan.position_size > 0 else 1.0
    size_scale = min(size_scale, same_symbol_scale)

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
            remaining_cluster_room_pct = MAX_CLUSTER_EXPOSURE_PCT - cluster_exposure_pct

            if remaining_cluster_room_pct <= 0:
                return PortfolioAdjustedPlan(
                    base_plan=base_plan, approved_size=0, approved_value=0.0, blocked=True,
                    block_reason=f"Correlated cluster ({', '.join(correlated_with)}) already at "
                                 f"{cluster_exposure_pct:.1f}% of capital (cap {MAX_CLUSTER_EXPOSURE_PCT}%) - "
                                 f"{symbol} moves with these, no room for more correlated exposure.",
                    total_risk_used_pct=total_risk_used_pct, cluster_exposure_pct=cluster_exposure_pct,
                    correlated_with=correlated_with,
                )

            max_value_by_cluster = capital * (remaining_cluster_room_pct / 100)
            max_size_by_cluster = int(max_value_by_cluster / base_plan.entry_price) if base_plan.entry_price > 0 else 0
            cluster_scale = min(1.0, max_size_by_cluster / base_plan.position_size) if base_plan.position_size > 0 else 1.0
            size_scale = min(size_scale, cluster_scale)

    approved_size = int(base_plan.position_size * size_scale)
    approved_value = round(approved_size * base_plan.entry_price, 2)

    return PortfolioAdjustedPlan(
        base_plan=base_plan, approved_size=approved_size, approved_value=approved_value,
        blocked=(approved_size == 0), block_reason=None if approved_size > 0 else "Scaled to zero by risk limits.",
        total_risk_used_pct=round(total_risk_used_pct, 2), cluster_exposure_pct=round(cluster_exposure_pct, 2),
        correlated_with=correlated_with,
    )


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
    print(f"  Cluster exposure: {plan.cluster_exposure_pct}% (correlated with: {plan.correlated_with})")
