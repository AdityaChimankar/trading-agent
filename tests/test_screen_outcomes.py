"""Historical screen outcomes: no look-ahead, trade returns, verdicts."""

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from trading_agent.research.screen_outcomes import SCREENS, OutcomeConfig, summarize, symbol_sessions

IST = timezone(timedelta(hours=5, minutes=30))
DECISION = 10 * 60 + 15


def session_frame(days: list[tuple[float, float, float]], start=date(2026, 9, 1), first_minute=0) -> pd.DataFrame:
    """Full sessions of minute bars: price ``before`` until 10:15, ``after`` from 10:15, ``last`` from 15:00."""
    rows, day = [], start
    for before, after, last in days:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        open_at = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
        for i in range(first_minute, 375):
            minute = 9 * 60 + 15 + i
            price = before if minute < DECISION else last if minute >= 15 * 60 else after
            rows.append({"timestamp": (open_at + timedelta(minutes=i)).astimezone(timezone.utc),
                         "open": price, "high": price + 0.5, "low": price - 0.5, "close": price, "volume": 1000})
        day += timedelta(days=1)
    return pd.DataFrame(rows)


def test_pivots_come_from_the_previous_session_and_returns_from_the_trade_window():
    frame = session_frame([(100, 100, 100), (110, 120, 132)])
    out = symbol_sessions("X", frame, DECISION)
    # Session 1 has no previous session; session 2's R1 = 2P - L of session 1 = 2*100 - 99.5 = 100.5.
    assert len(out) == 1
    row = out.iloc[0]
    assert row["resistance-breakout"]  # 110 at 10:14 > 100.5
    assert row["ret"] == pytest.approx(132 / 120 - 1)  # 10:15 open to the last bar before 15:15
    assert not row["macd-positive"] and not row["rsi-overbought"]  # too little history: never flagged


def test_today_does_not_leak_into_its_own_flags():
    # A huge rally after 10:15 on the last day must not change that day's flags.
    calm = session_frame([(100 + i % 3, 100 + i % 3, 100 + i % 3) for i in range(60)])
    rally = session_frame([(100 + i % 3, 100 + i % 3, 100 + i % 3) for i in range(59)] + [(102, 102, 300)])
    a, b = symbol_sessions("X", calm, DECISION), symbol_sessions("X", rally, DECISION)
    last = list(SCREENS)
    assert (a.iloc[-1][last] == b.iloc[-1][last]).all()
    assert b.iloc[-1]["ret"] > a.iloc[-1]["ret"]


def test_sessions_missing_the_open_are_dropped():
    frame = session_frame([(100, 100, 100), (100, 100, 100)], first_minute=30)  # first bar 09:45
    assert symbol_sessions("X", frame, DECISION).empty


def panel(edge: float, sessions: int = 90) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    rows = []
    for k in range(sessions):
        day = date(2026, 1, 1) + timedelta(days=k)
        market = rng.normal(0, 0.002)
        for i in range(40):
            flagged = i < 10
            ret = market + rng.normal(0, 0.001) + (edge if flagged else 0)
            rows.append({"session": day, "symbol": f"S{i}", "ret": ret, "avg_value_cr": 50.0,
                         **{name: flagged for name in SCREENS}})
    return pd.DataFrame(rows)


def test_verdicts_follow_net_edge_and_holdout_t():
    good = {s["id"]: s for s in summarize(panel(0.01))["screens"]}
    assert good["resistance-breakout"]["verdict"] == "edge_candidate"  # long, +100 bps a day
    assert good["support-breakdown"]["verdict"] == "no_edge"  # short side of the same names loses
    flat = {s["id"]: s for s in summarize(panel(0.0))["screens"]}
    assert flat["resistance-breakout"]["verdict"] in {"no_edge", "inconclusive"}
    assert flat["resistance-breakout"]["holdout"]["net"]["mean_bps"] < 0  # costs alone
    assert summarize(panel(0.01, sessions=10))["status"] == "insufficient_history"


def test_illiquid_names_are_excluded():
    p = panel(0.01)
    p["avg_value_cr"] = 1.0
    assert summarize(p, OutcomeConfig())["status"] == "insufficient_history"


def test_outcomes_route_reports_not_run(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from trading_agent.api.routers import screener as router_module
    from trading_agent.research import screen_outcomes

    monkeypatch.setattr(screen_outcomes, "read_report", lambda: None)
    app = FastAPI()
    app.include_router(router_module.router, prefix="/api/v1/screener")
    assert TestClient(app).get("/api/v1/screener/outcomes").json() == {"status": "not_run"}
