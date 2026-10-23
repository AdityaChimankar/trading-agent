"""Performance analytics on the closed paper/live book, net of trading costs.

The ledger records gross P&L. Net P&L subtracts the charges of the orders the
router actually filled for each position, or the modelled round-trip charges
(execution/costs.py) for positions entered manually without router orders.

Ratios use daily net P&L over the starting capital, annualised with 252
trading days. With few trading days these are noisy; ``sample_warning`` says so.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC
from zoneinfo import ZoneInfo

import numpy as np

from trading_agent.execution.costs import round_trip_charges

IST = ZoneInfo("Asia/Kolkata")
TRADING_DAYS = 252


def _aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def trade_rows(positions: list, order_charges: dict[int, float]) -> list[dict]:
    rows = []
    for p in positions:
        gross = float(p.pnl or 0.0)
        charges = order_charges.get(p.id)
        modelled = charges is None
        if modelled:
            charges = round_trip_charges(p.action, p.entry_price, p.exit_price, p.position_size)
        risk = float(p.risk_amount or 0.0)
        opened, closed = _aware(p.opened_at), _aware(p.exit_time)
        rows.append({
            "position_id": p.id, "symbol": p.symbol, "action": p.action, "strategy": p.signal_source or "unknown",
            "quantity": p.position_size, "entry_price": p.entry_price, "exit_price": p.exit_price,
            "opened_at": opened.isoformat(), "closed_at": closed.isoformat(),
            "holding_minutes": max(0, int((closed - opened).total_seconds() // 60)),
            "gross_pnl": round(gross, 2), "charges": round(charges, 2), "charges_modelled": modelled,
            "net_pnl": round(gross - charges, 2),
            "r_multiple": round(gross / risk, 3) if risk > 0 else None,
        })
    rows.sort(key=lambda r: r["closed_at"])
    return rows


def summarize(rows: list[dict], capital: float) -> dict:
    if not rows:
        return {"trades": 0}
    net = np.array([r["net_pnl"] for r in rows])
    gross = np.array([r["gross_pnl"] for r in rows])
    wins, losses = net[net > 0], net[net <= 0]
    equity = capital + np.cumsum(net)
    peak = np.maximum.accumulate(np.r_[capital, equity])[1:]
    drawdown = (peak - equity) / peak
    daily: dict[str, float] = defaultdict(float)
    for r in rows:
        daily[r["closed_at"][:10]] += r["net_pnl"]
    day_returns = np.array(list(daily.values())) / capital
    downside = day_returns[day_returns < 0]
    # A ratio needs a spread of daily outcomes: none below two trading days.
    std = day_returns.std(ddof=1) if len(day_returns) > 1 else 0.0
    down_std = math.sqrt(float(np.mean(downside ** 2))) if len(downside) and len(day_returns) > 1 else 0.0
    r_values = [r["r_multiple"] for r in rows if r["r_multiple"] is not None]
    return {
        "trades": len(rows),
        "win_rate_pct": round(len(wins) / len(net) * 100, 1),
        "gross_pnl": round(float(gross.sum()), 2),
        "charges": round(float(gross.sum() - net.sum()), 2),
        "net_pnl": round(float(net.sum()), 2),
        "net_return_pct": round(float(net.sum()) / capital * 100, 3),
        "avg_win": round(float(wins.mean()), 2) if len(wins) else None,
        "avg_loss": round(float(losses.mean()), 2) if len(losses) else None,
        "profit_factor": round(float(wins.sum() / -losses.sum()), 3) if losses.sum() < 0 else None,
        "expectancy": round(float(net.mean()), 2),
        "avg_r_multiple": round(float(np.mean(r_values)), 3) if r_values else None,
        "max_drawdown_pct": round(float(drawdown.max()) * 100, 3),
        "sharpe": round(float(day_returns.mean() / std * math.sqrt(TRADING_DAYS)), 3) if std > 0 else None,
        "sortino": round(float(day_returns.mean() / down_std * math.sqrt(TRADING_DAYS)), 3) if down_std > 0 else None,
        "trading_days": len(daily),
        "avg_holding_minutes": round(float(np.mean([r["holding_minutes"] for r in rows])), 1),
        # Share of gross profit eaten by charges; undefined when there is no gross profit.
        "cost_drag_pct_of_gross": (round(float((gross.sum() - net.sum()) / gross.sum() * 100), 1)
                                   if gross.sum() > 0 else None),
    }


def _group(rows: list[dict], key: str, capital: float) -> list[dict]:
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        groups[r[key]].append(r)
    out = [{key: name, **summarize(items, capital)} for name, items in groups.items()]
    return sorted(out, key=lambda g: -g["net_pnl"])


def equity_curve(rows: list[dict], capital: float) -> list[dict]:
    equity, out = capital, []
    for r in rows:
        equity += r["net_pnl"]
        out.append({"at": r["closed_at"], "equity": round(equity, 2)})
    return out


async def performance_report() -> dict:
    from sqlalchemy import func, select

    from trading_agent.core.config import get_settings
    from trading_agent.storage.db import Order, Position, get_database

    capital = get_settings().paper.initial_capital
    async with get_database().session() as session:
        closed = list((await session.execute(
            select(Position).where(Position.status.in_(["closed", "CLOSED"]), Position.exit_time.is_not(None))
        )).scalars())
        charges = dict((await session.execute(
            select(Order.position_id, func.sum(Order.charges)).where(Order.status == "FILLED")
            .group_by(Order.position_id))).all())
    rows = trade_rows(closed, charges)
    overall = summarize(rows, capital)
    warning = None
    if overall.get("trades", 0) and overall["trading_days"] < 20:
        warning = (f"Only {overall['trading_days']} trading day(s) of results: Sharpe, Sortino and win rate "
                   "are not statistically meaningful yet")
    return {"capital": capital, "overall": overall, "by_strategy": _group(rows, "strategy", capital),
            "by_symbol": _group(rows, "symbol", capital)[:25], "equity_curve": equity_curve(rows, capital),
            "recent_trades": rows[-50:][::-1], "sample_warning": warning}
