"""
/api/pipeline — how the three decision paths actually connect.

The repo runs three INDEPENDENT decision paths over the same candle:

    strategy/decision_agent.py       rule-based  -> signals
    strategy/ml_decision_agent.py    ML model    -> ml_signals
    strategy/llm_decision_agent.py   LLM agent   -> llm_signals

They are deliberately kept in separate tables (see storage/schema.sql) so each
one can be scored against the others before any of them is trusted with
capital. This endpoint does NOT merge them into a decision - it only shows,
side by side, what each path said on its latest candle for the symbols the
ranking is currently sizing, and whether they agreed.

Agreement is measured against the `rule_based_action` column the ML and LLM
cycles write at the same moment they write their own call. That column exists
precisely so the comparison is candle-aligned; comparing an ML call from one
cycle against whatever the rule path happens to say at request time would
measure clock skew, not agreement.

The last link in the chain is position SIZING, the only place a signal becomes
a rupee amount: core/position_sizing.py turns one action into a risk-based plan
and risk/portfolio_risk.py then shrinks or blocks it against what is already
open. Each row carries that final `sizing` block, so the signal -> size
connection is visible rather than implied.

Reuses rank_watchlist_with_sizing() (analysis/rankings.py) instead of
recomputing any of it, so this view can never disagree with the sidebar.
"""
from fastapi import APIRouter, Query

from api.serializers import jsonable
from storage.db import get_connection

router = APIRouter(prefix="/api", tags=["pipeline"])

# Table name -> the columns this view needs. Fixed constants, never user input
# (the same discipline api/routers/symbols.py uses when it builds these reads).
_AGENT_TABLES = {
    "signals": "timestamp, action, rsi, adx, atr, sentiment_score, rationale",
    "ml_signals": "timestamp, action, confidence, rule_based_action",
    "llm_signals": "timestamp, action, confidence, rule_based_action, rationale",
}


def _latest_per_symbol(conn, table: str, columns: str, symbols: list) -> dict:
    """Newest row per symbol from one agent table, in a single query.

    MAX(id) rather than MAX(timestamp): the two agent cycles stamp their rows
    with the CANDLE time (which repeats across cycles on the same minute), so
    timestamp alone cannot order two rows written inside one minute.
    """
    if not symbols:
        return {}
    placeholders = ",".join("?" * len(symbols))
    rows = conn.execute(
        f"SELECT t.symbol, {columns} FROM {table} t "
        f"JOIN (SELECT symbol, MAX(id) AS max_id FROM {table} "
        f"      WHERE symbol IN ({placeholders}) GROUP BY symbol) newest "
        f"  ON t.id = newest.max_id",
        symbols,
    ).fetchall()
    return {r["symbol"]: dict(r) for r in rows}


def _agent_block(row: dict | None, planned_action: str) -> dict | None:
    """One agent's latest call, annotated with what it is being compared to.

    `is_call` separates "this agent chose a direction" from "this agent sat
    out". A HOLD is an ABSTENTION, not a disagreement, so it must not be
    counted as the ML model arguing with the rules - opportunity_finder.py
    makes exactly the same distinction when it scores conviction, and the two
    views would contradict each other otherwise.
    """
    if row is None:
        return None
    return {
        **row,
        "is_call": row["action"] in ("BUY", "SELL"),
        "agrees_with_rule": (
            row.get("rule_based_action") is not None and row["action"] == row["rule_based_action"]
        ),
        "agrees_with_plan": row["action"] == planned_action,
    }


@router.get("/pipeline")
def pipeline(capital: float = Query(100000.0, gt=0), n: int = Query(10, ge=1, le=50)):
    """The signal chain, one row per symbol currently being sized."""
    from analysis.rankings import rank_watchlist_with_sizing
    from storage.db import get_open_position_symbols

    sized = rank_watchlist_with_sizing(capital, n=n)
    open_symbols = get_open_position_symbols()

    entries = (
        [("bullish", e) for e in sized["bullish"]]
        + [("bearish", e) for e in sized["bearish"]]
    )
    symbols = [e["symbol"] for _, e in entries]

    conn = get_connection()
    try:
        rules = _latest_per_symbol(conn, "signals", _AGENT_TABLES["signals"], symbols)
        ml_rows = _latest_per_symbol(conn, "ml_signals", _AGENT_TABLES["ml_signals"], symbols)
        llm_rows = _latest_per_symbol(conn, "llm_signals", _AGENT_TABLES["llm_signals"], symbols)
    finally:
        conn.close()

    rows = []
    for direction, entry in entries:
        symbol = entry["symbol"]
        action = entry["action"]

        rows.append({
            "symbol": symbol,
            "direction": direction,
            "action": action,
            "technical_score": entry["bullish_score"] if direction == "bullish" else entry["bearish_score"],
            "has_open_position": symbol in open_symbols,
            "rule": rules.get(symbol),
            "ml": _agent_block(ml_rows.get(symbol), action),
            "llm": _agent_block(llm_rows.get(symbol), action),
            "blocked": entry.get("blocked", False),
            "block_reason": entry.get("block_reason"),
            "sizing": {
                "entry_price": entry.get("entry_price"),
                "stop_loss": entry.get("stop_loss"),
                "take_profit": entry.get("take_profit"),
                "suggested_size": entry.get("suggested_size"),
                "suggested_value": entry.get("suggested_value"),
                "total_risk_used_pct": entry.get("total_risk_used_pct"),
                "correlated_with": entry.get("correlated_with") or [],
            },
        })

    # Only genuine directional calls are comparable. Counting a HOLD as
    # disagreement would report a "0% agreement" feed whenever the agents
    # mostly sit out, which reads as conflict and is actually the opposite.
    def _comparable(pair: str) -> list:
        return [
            r for r in rows
            if r[pair] and r[pair]["is_call"] and r[pair]["rule_based_action"] is not None
        ]

    ml_cmp = _comparable("ml")
    llm_cmp = _comparable("llm")

    def _pct(matched: list, total: list) -> float | None:
        return round(len(matched) / len(total) * 100, 1) if total else None

    summary = {
        "symbols": len(rows),
        "sized": sum(1 for r in rows if r["sizing"]["suggested_size"]),
        "blocked": sum(1 for r in rows if r["blocked"]),
        "open_positions": sum(1 for r in rows if r["has_open_position"]),
        "ml_calls": sum(1 for r in rows if r["ml"] and r["ml"]["is_call"]),
        "ml_compared": len(ml_cmp),
        "ml_agreement_pct": _pct([r for r in ml_cmp if r["ml"]["agrees_with_rule"]], ml_cmp),
        "llm_calls": sum(1 for r in rows if r["llm"] and r["llm"]["is_call"]),
        "llm_compared": len(llm_cmp),
        "llm_agreement_pct": _pct([r for r in llm_cmp if r["llm"]["agrees_with_rule"]], llm_cmp),
    }

    return jsonable({"summary": summary, "rows": rows})
