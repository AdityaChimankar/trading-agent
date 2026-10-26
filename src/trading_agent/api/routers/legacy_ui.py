"""Classic dashboard API (``/api/*``) on top of the current engine.

The original React dashboard (Opportunity panel, Position Monitor, Wallet,
Symbol detail, News, Pipeline, History) spoke to an older ``/api`` surface.
These endpoints serve the same response shapes from today's single sources
of truth so both UIs show the same numbers:

- rankings / opportunities / sizing / pipeline -> the latest research scan
  snapshot (one ranked, basket-sized pass; nothing re-ranks per request)
- chart / patterns -> the same completed-candle frame the research view uses
- positions / wallet / history -> the persisted paper ledger, opened and
  closed through the /api/v1 position endpoints so every risk check applies
- signals / llm-signals / ml-signals -> the ``signals`` table by strategy

Anything the current ledger does not record (cash deposits/withdrawals) is
refused explicitly rather than faked.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from trading_agent.api.serializers import jsonable as to_jsonable
from trading_agent.storage.db import Position, Signal, get_database

router = APIRouter()
IST = ZoneInfo("Asia/Kolkata")
DEFAULT_INTERVAL = "minute"


# --- shared helpers ----------------------------------------------------------

def _scan() -> dict:
    from trading_agent.analysis.research_jobs import get_research_jobs

    # The classic dashboard keeps the last finished scan on screen while a new one runs.
    return get_research_jobs().snapshot(completed=True)


def _scan_rows() -> list[dict]:
    return [row for row in _scan().get("rows", []) if row.get("action") in {"BUY", "SELL"}]


def _now() -> datetime:
    return datetime.now(UTC)


def _fresh_plan(row: dict) -> dict | None:
    """The scan's sized plan if it is still actionable right now."""
    plan = row.get("plan")
    expires = row.get("expires_at")
    if not plan or plan.get("blocked") or not plan.get("quantity"):
        return None
    if expires and datetime.fromisoformat(expires) <= _now():
        return None
    return plan


def _trend(row: dict) -> str:
    return {"BUY": "up", "SELL": "down"}.get(row.get("action"), "flat")


async def _open_positions() -> list[Position]:
    async with get_database().session() as session:
        result = await session.execute(select(Position).where(Position.status.in_(["open", "OPEN"])))
        return list(result.scalars())


def _position_dict(p: Position) -> dict:
    return {"id": p.id, "symbol": p.symbol, "action": p.action, "entry_price": p.entry_price,
            "position_size": p.position_size, "position_value": p.position_value,
            "risk_amount": p.risk_amount, "stop_loss": p.stop_loss, "take_profit": p.take_profit,
            "opened_at": _utc(p.opened_at), "status": p.status}


def _utc(stamp: datetime | None) -> datetime | None:
    """SQLite returns naive UTC; an explicit offset stops browsers reading it as local time."""
    return stamp.replace(tzinfo=UTC) if stamp is not None and stamp.tzinfo is None else stamp


async def _statuses() -> list[dict]:
    from trading_agent.risk.position_monitor import get_position_status

    out = []
    for p in await _open_positions():
        status = await get_position_status(p, fetch_current_signals=False)
        out.append({
            "position": _position_dict(p),
            "current_price": status.current_price, "current_value": status.current_value,
            "pnl": status.pnl, "pnl_pct": status.pnl_pct,
            "recommendation": status.recommendation, "reason": status.reason,
            "stale": status.stale, "candle_age_minutes": status.candle_age_minutes,
            "candle_timestamp": None,
        })
    return out


def _ranked_entry(row: dict, held: dict[str, dict]) -> dict:
    status = held.get(row["symbol"])
    pnl = status["pnl"] if status else None
    bias = "bullish" if row["action"] == "BUY" else "bearish"
    contradiction = None
    if status and pnl is not None and status["position"]["action"] != row["action"]:
        side = "profit" if pnl > 0 else "loss"
        contradiction = (f"Open {status['position']['action']} is in {side} (INR {pnl:,.0f}) "
                         f"but the scan now reads {bias}")
    plan = row.get("plan") or {}
    return {
        "symbol": row["symbol"], "rsi": row.get("rsi"), "adx": row.get("adx"), "trend": _trend(row),
        "pattern_bias": row.get("pattern_bias", "neutral"), "patterns": row.get("patterns", []),
        "bullish_score": row["score"] if row["action"] == "BUY" else 0.0,
        "bearish_score": row["score"] if row["action"] == "SELL" else 0.0,
        "has_open_position": status is not None, "dominant_bias": bias,
        "contradiction": contradiction, "action": row["action"],
        "entry_price": plan.get("entry_price"), "stop_loss": plan.get("stop_loss"),
        "take_profit": plan.get("take_profit"), "suggested_size": plan.get("quantity"),
        "suggested_value": plan.get("position_value"), "blocked": _fresh_plan(row) is None,
        "block_reason": None if _fresh_plan(row) else (
            plan.get("block_reason") or ("Plan expired; re-scan" if plan else "Not sized in the latest scan")),
        "total_risk_used_pct": None, "correlated_with": [],
    }


def _require_symbol(symbol: str) -> str:
    from trading_agent.analysis.research_news import news_path

    symbol = symbol.strip().upper()
    try:
        news_path(symbol)  # same validation as the research API
    except ValueError as error:
        raise HTTPException(422, "Invalid symbol") from error
    return symbol


# --- watchlist / rankings / opportunities -------------------------------------

@router.get("/watchlist")
def watchlist():
    from trading_agent.analysis.research_engine import universe

    return {"symbols": universe()}


@router.get("/rankings")
async def rankings(capital: float = Query(100_000.0, gt=0)):
    """Watchlist bias from the latest scan, with open-position contradictions.

    ``capital`` is accepted for compatibility; sizing comes from the scan's own
    capital so the sidebar and the Opportunity panel never disagree.
    """
    rows = await run_in_threadpool(_scan_rows)
    held = {s["position"]["symbol"]: s for s in await _statuses()}
    scored = [_ranked_entry(row, held) for row in rows]
    # Like the original sized lists, blocked names stay in (flagged) so the
    # sidebar still shows the bias with the reason it can't be traded.
    return to_jsonable({
        "scored": scored,
        "bullish": [e for e in scored if e["action"] == "BUY"][:10],
        "bearish": [e for e in scored if e["action"] == "SELL"][:10],
        "contradictions": [e for e in scored if e["contradiction"]],
    })


def _opportunity(row: dict) -> dict:
    plan = row.get("plan") or {}
    fresh = _fresh_plan(row)
    blocked = fresh is None
    reason = plan.get("block_reason") or ("Plan expired; re-scan" if plan else "Not sized in the latest scan")
    return {
        "symbol": row["symbol"], "action": row["action"],
        "conviction_score": row["score"],
        "technical_score": round(float(row["rule"]["confidence"]) * 100, 1),
        "agreements": row.get("agreements", []), "disagreements": row.get("disagreements", []),
        "entry_price": plan.get("entry_price"), "stop_loss": plan.get("stop_loss"),
        "take_profit": plan.get("take_profit"),
        "suggested_size": plan.get("quantity"), "suggested_value": plan.get("position_value"),
        "blocked": blocked, "block_reason": reason if blocked else None,
        "room_to_target": row.get("room_to_target"),
        "realistic_reward_risk": plan.get("room_reward_risk"),
    }


@router.get("/opportunities")
async def opportunities(capital: float | None = Query(None, gt=0), n: int = Query(10, ge=1, le=50)):
    from trading_agent.risk.paper_ledger import load_paper_book

    scan = await run_in_threadpool(_scan)
    held = {p.symbol for p in await _open_positions()}
    rows = [r for r in scan.get("rows", []) if r.get("action") in {"BUY", "SELL"}]
    picks = [r for r in rows if r["symbol"] not in held]
    book = await load_paper_book()
    return to_jsonable({
        "bullish": [_opportunity(r) for r in picks if r["action"] == "BUY"][:n],
        "bearish": [_opportunity(r) for r in picks if r["action"] == "SELL"][:n],
        "excluded": sorted({r["symbol"] for r in rows if r["symbol"] in held}),
        "available_capital": round(scan.get("effective_capital") or book.cash, 2),
    })


# --- positions -----------------------------------------------------------------

class OpenPositionRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    action: str = Field(pattern="^(BUY|SELL)$")
    capital: float = Field(gt=0)


@router.get("/positions")
async def list_positions():
    """Every open paper position with live P&L and a HOLD/SELL/STALE/NO_DATA call."""
    return to_jsonable(await _statuses())


@router.post("/positions", status_code=201)
async def open_position(payload: OpenPositionRequest):
    """Enter the scan's sized plan through the order router (all risk checks apply)."""
    from trading_agent.execution.router import get_order_router

    symbol = _require_symbol(payload.symbol)
    rows = await run_in_threadpool(_scan_rows)
    row = next((r for r in rows if r["symbol"] == symbol and r["action"] == payload.action), None)
    plan = _fresh_plan(row) if row else None
    if plan is None:
        reason = (row or {}).get("plan", {}).get("block_reason") if row else None
        raise HTTPException(400, reason or "No fresh, unblocked plan for this symbol and side. Re-scan first.")
    filled = await get_order_router().enter(
        symbol=symbol, side=payload.action, quantity=int(plan["quantity"]),
        reference_price=plan["entry_price"], stop_loss=plan["stop_loss"],
        take_profit=plan["take_profit"], strategy="hybrid_scan", reason="classic_dashboard")
    return {"symbol": symbol, "action": payload.action, "shares": filled["quantity"],
            "value": round(filled["fill_price"] * filled["quantity"], 2), "mode": filled["mode"]}


async def _latest_close(symbol: str) -> float:
    from trading_agent.core.config import get_settings
    from trading_agent.storage.parquet_store import get_parquet_store

    frame = await run_in_threadpool(get_parquet_store().read_latest, symbol,
                                    get_settings().storage.candle_interval, 1)
    if frame.empty:
        raise HTTPException(409, f"No stored candle for {symbol}; close it from the paper workspace with an explicit exit price")
    return float(frame["close"].iloc[-1])


@router.post("/positions/{position_id}/close")
async def close(position_id: int):
    """Exit through the order router at the latest stored price (slippage applies)."""
    from trading_agent.execution.router import get_order_router

    async with get_database().session() as session:
        position = await session.get(Position, position_id)
    if position is None:
        raise HTTPException(404, "Position not found.")
    exit_price = await _latest_close(position.symbol)
    result = await get_order_router().exit_position(position_id, reason="classic_dashboard", price=exit_price)
    return {"closed": position_id, "symbol": position.symbol,
            "realized_pnl_rupees": result["gross_pnl"], "exit_price": result["exit_price"]}


# --- wallet / history ------------------------------------------------------------

@router.get("/wallet")
async def wallet():
    from trading_agent.risk.paper_ledger import load_paper_book

    book = await load_paper_book()
    statuses = await _statuses()
    today = datetime.now(IST).date()
    open_rows = []
    for s in statuses:
        p = s["position"]
        price = s["current_price"]
        sign = 1 if p["action"] == "BUY" else -1
        qty = p["position_size"]

        def scenario(level, sign=sign, qty=qty, entry=p["entry_price"]):
            return None if level is None else round(sign * (level - entry) * qty, 2)

        open_rows.append({
            "position_id": p["id"], "symbol": p["symbol"], "action": p["action"], "position_size": qty,
            "entry_price": p["entry_price"], "current_price": price, "current_value": s["current_value"],
            "current_pnl": s["pnl"], "current_pnl_pct": s["pnl_pct"],
            "stop_loss": p["stop_loss"], "take_profit": p["take_profit"],
            "stop_scenario_pnl": scenario(p["stop_loss"]), "target_scenario_pnl": scenario(p["take_profit"]),
            "at_risk_rupees": p["risk_amount"],
        })
    closed_by_day: dict[str, float] = defaultdict(float)
    for trade in book.trades:
        closed_by_day[trade.exit_time.astimezone(IST).date().isoformat()] += trade.net_pnl
    open_pnl = sum(r["current_pnl"] or 0 for r in open_rows)
    closed_today = closed_by_day.pop(today.isoformat(), 0.0)
    return to_jsonable({
        "fixed": {"capital": book.initial_cash, "total_deposits": 0.0, "total_withdrawals": 0.0,
                  "realized_pnl_total": book.total_pnl, "book_balance": book.initial_cash + book.total_pnl},
        "today": {"open_pnl": open_pnl, "open_positions_count": len(open_rows),
                  "closed_pnl": closed_today, "total_today_pnl": open_pnl + closed_today},
        "open_positions": open_rows,
        "prev_days_closed_pnl": dict(sorted(closed_by_day.items(), reverse=True)),
    })


@router.post("/wallet/close-position")
async def wallet_close(position_id: int = Query(...)):
    return await close(position_id)


@router.post("/wallet/deposit")
@router.post("/wallet/withdraw")
async def wallet_cash_movement():
    raise HTTPException(409, "The paper book is derived from positions and PAPER_INITIAL_CAPITAL; "
                             "change the starting capital in .env instead of depositing or withdrawing")


@router.get("/transactions")
async def transactions(limit: int = Query(200, ge=1, le=2000)):
    from trading_agent.risk.paper_ledger import load_paper_book

    book = await load_paper_book()
    async with get_database().session() as session:
        closed = list((await session.execute(
            select(Position).where(Position.status.in_(["closed", "CLOSED"])).order_by(Position.exit_time.desc())
        )).scalars())[:limit]
    trades = [{
        "id": p.id, "symbol": p.symbol, "action": p.action, "position_id": p.id,
        "entry_price": p.entry_price, "entry_at": _utc(p.opened_at), "exit_price": p.exit_price,
        "exit_at": _utc(p.exit_time), "position_size": p.position_size,
        "realized_pnl_rupees": float(p.pnl or 0), "exit_reason": "closed", "slippage_deducted": 0.0,
    } for p in closed]
    ledger = [{"id": 0, "type": "deposit", "amount": book.initial_cash, "note": "Paper starting capital",
               "symbol": None, "trade_id": None, "transacted_at": None}]
    ledger += [{"id": t["id"], "type": "realized_pnl", "amount": t["realized_pnl_rupees"],
                "note": f"{t['action']} {t['position_size']} closed", "symbol": t["symbol"],
                "trade_id": t["id"], "transacted_at": t["exit_at"]} for t in trades]
    daily: dict[str, list] = defaultdict(list)
    for t in trades:
        if t["exit_at"]:
            daily[t["exit_at"].astimezone(IST).date().isoformat()].append(t["realized_pnl_rupees"])
    pnls = [t["realized_pnl_rupees"] for t in trades]
    wins, losses = [x for x in pnls if x > 0], [x for x in pnls if x <= 0]
    best = max(trades, key=lambda t: t["realized_pnl_rupees"], default=None)
    worst = min(trades, key=lambda t: t["realized_pnl_rupees"], default=None)

    def brief(t):
        if t is None:
            return None
        return {"symbol": t["symbol"], "realized_pnl_rupees": t["realized_pnl_rupees"], "exit_at": t["exit_at"]}

    exits = [t["exit_at"] for t in trades if t["exit_at"]]
    return to_jsonable({
        "summary": {
            "deposits": book.initial_cash, "withdrawals": 0.0, "realized_pnl": sum(pnls),
            "net": book.initial_cash + sum(pnls), "transaction_count": len(ledger), "trade_count": len(trades),
            "wins": len(wins), "losses": len(losses),
            "win_rate_pct": round(len(wins) / len(pnls) * 100, 1) if pnls else None,
            "gross_profit": sum(wins), "gross_loss": sum(losses), "slippage_total": 0.0,
            "best": brief(best), "worst": brief(worst),
            "first_at": min(exits) if exits else None, "last_at": max(exits) if exits else None,
        },
        "ledger": ledger,
        "trades": trades,
        "daily": [{"date": d, "trades": len(v), "closed_pnl": sum(v)} for d, v in sorted(daily.items(), reverse=True)],
    })


# --- digest ----------------------------------------------------------------------

DIGEST_SYMBOLS = 30
DIGEST_TTL_SECONDS = 900
_digest_cache: dict[str, tuple[float, dict]] = {}


@router.get("/digest")
async def digest(date: str | None = Query(None)):
    """Narrative digest over the latest scan's strongest names.

    The generator analyses every symbol it is given, so it is bounded to the
    scan's top directional rows and cached briefly; a full-universe digest
    took minutes per request.
    """
    import time
    from datetime import date as date_type

    try:
        target = date_type.fromisoformat(date) if date else datetime.now(IST).date()
    except ValueError as error:
        raise HTTPException(422, "date must be YYYY-MM-DD") from error
    key = target.isoformat()
    cached = _digest_cache.get(key)
    fresh = cached and time.monotonic() - cached[0] < DIGEST_TTL_SECONDS
    if not fresh and key not in _digest_building:
        _digest_building.add(key)
        threading.Thread(target=_build_digest, args=(key, target), name="digest", daemon=True).start()
    if cached:
        return cached[1]  # serve the previous digest while a refresh builds
    return {"date": key, "summary": None, "generated_at": None, "status": "generating"}


_digest_building: set[str] = set()


def _build_digest(key: str, target) -> None:
    import time

    from trading_agent.analysis.digest import DigestGenerator

    try:
        rows = sorted(_scan_rows(), key=lambda r: -r["score"])[:DIGEST_SYMBOLS]
        summary = None
        if rows:
            generator = DigestGenerator(symbols=[r["symbol"] for r in rows])
            summary = generator.format_digest(generator.generate(target))
        _digest_cache[key] = (time.monotonic(), {
            "date": key, "summary": summary, "generated_at": _now().isoformat() if summary else None})
    except Exception:
        _digest_cache[key] = (time.monotonic(), {"date": key, "summary": None, "generated_at": None})
    finally:
        _digest_building.discard(key)


# --- per-symbol views --------------------------------------------------------------

def _research_frame(symbol: str, interval: str):
    from trading_agent.analysis.research_engine import completed_frame
    from trading_agent.storage.parquet_store import get_parquet_store
    from trading_agent.strategy.research_features import prepare_research_frame

    frame = completed_frame(get_parquet_store(), symbol, interval, _now())
    return prepare_research_frame(frame) if len(frame) >= 60 else None


@router.get("/symbols/news")
def all_news(limit: int = Query(100, ge=1, le=500)):
    from trading_agent.analysis.research_engine import universe
    from trading_agent.core.config import get_settings

    # One directory listing, not an exists() per watchlist symbol: 2,300 of those
    # took 12 s on the Docker Desktop bind mount for a dozen news files.
    directory = get_settings().data_dir / "research" / "news"
    stored = {path.stem for path in directory.glob("*.json")} if directory.is_dir() else set()
    rows = []
    for symbol in universe():
        if symbol in stored:
            rows.extend(_news_rows(symbol))
    rows.sort(key=lambda r: r["published_at"] or "", reverse=True)
    return rows[:limit]


def _news_rows(symbol: str) -> list[dict]:
    from trading_agent.analysis.research_news import read_news

    try:
        news = read_news(symbol)
    except (ValueError, OSError):
        return []
    analysis = news.get("analysis") or {}
    return [{"symbol": symbol, "headline": a["headline"], "published_at": a.get("published_at"),
             "source": a.get("source"), "score": analysis.get("score"),
             "rationale": analysis.get("rationale")} for a in news.get("articles", [])]


@router.get("/symbols/{symbol}/chart")
async def chart(symbol: str, limit: int = Query(200, ge=20, le=2000), interval: str = Query(DEFAULT_INTERVAL)):
    from trading_agent.analysis.research_engine import INTERVAL_SECONDS, chart_payload

    symbol = _require_symbol(symbol)
    if interval not in INTERVAL_SECONDS:
        raise HTTPException(422, "Unsupported interval")
    df = await run_in_threadpool(_research_frame, symbol, interval)
    if df is None:
        return {"symbol": symbol, "insufficient_data": True, "candles": None}
    payload = chart_payload(symbol, df, limit)
    payload["candles"]["timestamp"] = [t.isoformat() for t in payload["candles"]["timestamp"]]
    for marker in payload["signal_markers"]:
        marker["timestamp"] = marker["timestamp"].isoformat()
    return to_jsonable(payload)


@router.get("/symbols/{symbol}/patterns")
async def patterns(symbol: str, interval: str = Query(DEFAULT_INTERVAL)):
    symbol = _require_symbol(symbol)
    df = await run_in_threadpool(_research_frame, symbol, interval)
    if df is None:
        return {"patterns": [], "bias": "neutral"}
    last = df.iloc[-1]
    names = [n for n in ("doji", "hammer", "bullish_engulfing", "bearish_engulfing") if bool(last[n])]
    return {"patterns": names, "bias": ["neutral", "bullish", "bearish"][int(last.pattern_code)]}


@router.get("/symbols/{symbol}/technicals")
async def technicals(symbol: str, interval: str = Query(DEFAULT_INTERVAL)):
    from trading_agent.analysis.research_engine import INTERVAL_SECONDS
    from trading_agent.analysis.technical_summary import summary_from_frame

    symbol = _require_symbol(symbol)
    if interval not in INTERVAL_SECONDS:
        raise HTTPException(422, "Unsupported interval")
    df =await run_in_threadpool(_research_frame, symbol, interval)
    summary = summary_from_frame(symbol, interval, df) if df is not None else None
    return to_jsonable(summary or {"symbol": symbol, "insufficient_data": True})


@router.get("/symbols/{symbol}/sizing")
async def sizing(symbol: str, capital: float = Query(100_000.0, gt=0)):
    symbol = _require_symbol(symbol)
    rows = await run_in_threadpool(_scan_rows)
    row = next((r for r in rows if r["symbol"] == symbol), None)
    if row is None or not row.get("plan"):
        return {"symbol": symbol, "has_signal": False, "action": row["action"] if row else None, "adjusted": None}
    plan = row["plan"]
    fresh = _fresh_plan(row)
    distance = abs(plan["entry_price"] - plan["stop_loss"])
    return to_jsonable({"symbol": symbol, "has_signal": True, "action": row["action"], "adjusted": {
        "base_plan": {"symbol": symbol, "action": row["action"], "entry_price": plan["entry_price"],
                      "atr": row["atr"], "stop_loss": plan["stop_loss"], "take_profit": plan["take_profit"],
                      "stop_distance": distance, "risk_amount": plan["risk_amount"],
                      "position_size": plan["quantity"], "position_value": plan["position_value"],
                      "capped_by_max_position": False},
        "approved_size": plan["quantity"] if fresh else 0,
        "approved_value": plan["position_value"] if fresh else 0.0,
        "blocked": fresh is None,
        "block_reason": None if fresh else (plan.get("block_reason") or "Plan expired; re-scan"),
        "total_risk_used_pct": 0.0, "cluster_exposure_pct": 0.0, "correlated_with": [],
    }})


@router.get("/symbols/{symbol}/news")
def symbol_news(symbol: str, limit: int = Query(10, ge=1, le=100)):
    return _news_rows(_require_symbol(symbol))[:limit]


async def _signals(symbol: str, strategy: str, limit: int) -> list[Signal]:
    async with get_database().session() as session:
        result = await session.execute(
            select(Signal).where(Signal.symbol == symbol, Signal.strategy == strategy)
            .order_by(Signal.timestamp.desc()).limit(limit))
        return list(result.scalars())


async def _latest_signal_by_symbol(strategy: str) -> dict[str, Signal]:
    """Newest signal per symbol for one strategy, in a single query."""
    from sqlalchemy import func

    newest = (select(Signal.symbol, func.max(Signal.timestamp).label("ts"))
              .where(Signal.strategy == strategy).group_by(Signal.symbol).subquery())
    async with get_database().session() as session:
        result = await session.execute(
            select(Signal).join(newest, (Signal.symbol == newest.c.symbol) & (Signal.timestamp == newest.c.ts))
            .where(Signal.strategy == strategy))
        return {s.symbol: s for s in result.scalars()}


@router.get("/symbols/{symbol}/signals")
async def rule_signals(symbol: str, limit: int = Query(20, ge=1, le=200)):
    rows = await _signals(_require_symbol(symbol), "rule", limit)
    return to_jsonable([{"timestamp": s.timestamp, "action": s.action, "rsi": s.rsi, "adx": s.adx,
                         "sentiment_score": None, "rationale": None} for s in rows])


async def _agent_rows(symbol: str, strategy: str, limit: int) -> tuple[list[Signal], list[Signal | None]]:
    """Agent rows paired with the rule call nearest before each (the comparison baseline)."""
    agent = await _signals(symbol, strategy, limit)
    rules = await _signals(symbol, "rule", limit * 4)
    paired = []
    for s in agent:
        prior = next((r for r in rules if r.timestamp <= s.timestamp), None)
        paired.append(prior)
    return agent, paired


def _agreement(agent: list[Signal], rules: list[Signal | None]) -> float | None:
    compared = [(a, r) for a, r in zip(agent, rules, strict=True)
                if r is not None and a.action != "HOLD" and r.action != "HOLD"]
    return round(sum(a.action == r.action for a, r in compared) / len(compared) * 100, 1) if compared else None


@router.get("/symbols/{symbol}/llm-signals")
async def llm_signals(symbol: str, limit: int = Query(20, ge=1, le=200)):
    agent, rules = await _agent_rows(_require_symbol(symbol), "llm", limit)
    return to_jsonable({"agreement_pct": _agreement(agent, rules), "rows": [
        {"timestamp": a.timestamp, "llm_action": a.action, "rule_based_action": r.action if r else None,
         "confidence": a.confidence, "rationale": a.signal_metadata} for a, r in zip(agent, rules, strict=True)]})


@router.get("/symbols/{symbol}/ml-signals")
async def ml_signals(symbol: str, limit: int = Query(20, ge=1, le=200)):
    agent, rules = await _agent_rows(_require_symbol(symbol), "ml", limit)
    return to_jsonable({"agreement_pct": _agreement(agent, rules), "rows": [
        {"timestamp": a.timestamp, "ml_action": a.action, "rule_based_action": r.action if r else None,
         "confidence": a.confidence} for a, r in zip(agent, rules, strict=True)]})


# --- pipeline ----------------------------------------------------------------------

def _agent_signal(action: str | None, confidence, rule_action: str, plan_action: str) -> dict | None:
    if action is None:
        return None
    is_call = action in {"BUY", "SELL"}
    return {"timestamp": None, "action": action, "confidence": confidence,
            "rule_based_action": rule_action, "is_call": is_call,
            "agrees_with_rule": is_call and action == rule_action,
            "agrees_with_plan": is_call and action == plan_action}


@router.get("/pipeline")
async def pipeline(capital: float = Query(100_000.0, gt=0)):
    """Every scanned symbol through rules -> ML -> LLM -> sizing, side by side."""
    scan = await run_in_threadpool(_scan)
    held = {p.symbol for p in await _open_positions()}
    latest_llm = await _latest_signal_by_symbol("llm")
    rows = []
    for row in scan.get("rows", []):
        if row.get("action") not in {"BUY", "SELL"}:
            continue
        rule_action = row["rule"]["action"]
        ml = row.get("ml") or {}
        llm = latest_llm.get(row["symbol"])
        plan = row.get("plan") or {}
        fresh = _fresh_plan(row)
        rows.append({
            "symbol": row["symbol"], "direction": "bullish" if row["action"] == "BUY" else "bearish",
            "action": row["action"], "technical_score": round(float(row["rule"]["confidence"]) * 100, 1),
            "has_open_position": row["symbol"] in held,
            "rule": {"timestamp": row["timestamp"], "action": rule_action, "rsi": row.get("rsi"),
                     "adx": row.get("adx"), "atr": row.get("atr"), "sentiment_score":
                     (row.get("sentiment") or {}).get("score"), "rationale": row.get("reasoning")},
            "ml": _agent_signal(ml.get("action"), ml.get("confidence"), rule_action, row["action"]),
            "llm": _agent_signal(llm.action, llm.confidence, rule_action, row["action"]) if llm else None,
            "blocked": fresh is None,
            "block_reason": None if fresh else (plan.get("block_reason") or "Not sized / expired"),
            "sizing": {"entry_price": plan.get("entry_price"), "stop_loss": plan.get("stop_loss"),
                       "take_profit": plan.get("take_profit"), "suggested_size": plan.get("quantity"),
                       "suggested_value": plan.get("position_value"), "total_risk_used_pct": None,
                       "correlated_with": []},
        })

    def stats(key):
        calls = [r[key] for r in rows if r[key] and r[key]["is_call"]]
        compared = [c for c in calls if c["rule_based_action"] in {"BUY", "SELL"}]
        agree = sum(c["agrees_with_rule"] for c in compared)
        return len(calls), len(compared), (round(agree / len(compared) * 100, 1) if compared else None)

    ml_calls, ml_compared, ml_pct = stats("ml")
    llm_calls, llm_compared, llm_pct = stats("llm")
    return to_jsonable({
        "summary": {"symbols": len(rows), "sized": sum(not r["blocked"] for r in rows),
                    "blocked": sum(r["blocked"] for r in rows), "open_positions": len(held),
                    "ml_calls": ml_calls, "ml_compared": ml_compared, "ml_agreement_pct": ml_pct,
                    "llm_calls": llm_calls, "llm_compared": llm_compared, "llm_agreement_pct": llm_pct},
        "rows": rows,
    })
