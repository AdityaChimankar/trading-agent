from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from trading_agent.core.config import PaperRiskSettings
from trading_agent.risk import ledger_guard as guard
from trading_agent.analysis import research_jobs as jobs

NOW = datetime(2026, 10, 20, 8, tzinfo=UTC)


def closed(ident, pnl, stamp=NOW - timedelta(minutes=1)):
    return {"id": ident, "status": "closed", "pnl": pnl, "exit_time": stamp.isoformat()}


def test_loss_limit_uses_ist_session_start_equity_and_blocks_at_boundary():
    limits = PaperRiskSettings(initial_capital=100000)
    old = closed(1, 10000, NOW - timedelta(days=1))
    result = guard.ledger_state([old, closed(2, -3300)], limits, NOW)
    assert result["realized_equity"] == 106700
    assert result["daily_realized_pnl"] == -3300
    assert not result["entries_allowed"]
    assert "daily-loss" in result["block_reason"]
    result = guard.ledger_state([old, closed(2, -3299)], limits, NOW)
    assert result["entries_allowed"]


def test_realized_drawdown_survives_new_session_and_operator_disable():
    limits = PaperRiskSettings(initial_capital=100000)
    result = guard.ledger_state([closed(1, 10000, NOW-timedelta(days=3)),
                                closed(2, -17000, NOW-timedelta(days=2))], limits, NOW)
    assert result["daily_realized_pnl"] == 0
    assert result["realized_drawdown_pct"] == pytest.approx(17000 / 110000 * 100)
    assert "drawdown" in result["block_reason"]
    result = guard.ledger_state([], limits.model_copy(update={"entries_enabled": False}), NOW)
    assert not result["entries_allowed"]


def test_invalid_evidence_fails_closed_and_fingerprint_binds_limits_and_day():
    limits = PaperRiskSettings()
    for row in (closed(1, float("nan")), closed(1, None), closed(1, 1, NOW+timedelta(hours=1))):
        with pytest.raises((TypeError, ValueError)):
            guard.ledger_state([row], limits, NOW)
    baseline = guard.ledger_state([], limits, NOW)["fingerprint"]
    assert baseline != guard.ledger_state([], limits, NOW+timedelta(days=1))["fingerprint"]
    assert baseline != guard.ledger_state([closed(1, 1)], limits, NOW)["fingerprint"]
    assert baseline != guard.ledger_state([], limits.model_copy(update={"max_trade_risk_pct": .01}), NOW)["fingerprint"]


def test_readonly_missing_ledger_is_not_created(tmp_path, monkeypatch):
    path = tmp_path / "missing.db"
    monkeypatch.setattr(guard, "get_settings", lambda: SimpleNamespace(
        database=SimpleNamespace(url=f"sqlite:///{path.as_posix()}"), paper=PaperRiskSettings()))
    with pytest.raises(Exception):
        guard.read_ledger_state()
    assert not path.exists()


def test_cross_client_book_changes_invalidate_existing_scan_plans(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    state = {"fingerprint": "original", "entries_allowed": True, "block_reason": None}
    monkeypatch.setattr(jobs, "read_ledger_state", lambda *args: state)
    job = jobs.ResearchJobs()
    job.state.update(ledger_fingerprint="original", rows=[{
        "symbol": "TEST", "stale": False, "validation": {"status": "unqualified"},
        "plan": {"blocked": False, "quantity": 10, "position_value": 1000, "risk_amount": 20},
    }])
    assert job.snapshot()["rows"][0]["plan"]["quantity"] == 10
    state["fingerprint"] = "another-client-changed-book"
    result = job.snapshot()
    assert result["eligible"] == 0
    assert result["rows"][0]["plan"]["quantity"] == 0
    assert "re-scan" in result["allocation_warning"]
    # Underlying historical result is retained; only derived allocation is blocked.
    assert job.state["rows"][0]["plan"]["quantity"] == 10


def test_api_new_entries_blocked_but_exits_allowed_after_realized_loss(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from trading_agent.api.main import create_app
    from trading_agent.core.config import get_settings
    from trading_agent.storage import db
    path = tmp_path / "ledger.db"
    monkeypatch.setenv("DB_URL", f"sqlite:///{path.as_posix()}")
    monkeypatch.setenv("PAPER_INITIAL_CAPITAL", "100000")
    monkeypatch.setenv("PAPER_MAX_DAILY_LOSS_PCT", "0.03")
    monkeypatch.setenv("PAPER_MAX_SINGLE_POSITION_PCT", "0.10")
    monkeypatch.setenv("PAPER_ENTRIES_ENABLED", "true")
    monkeypatch.setenv("API_AUTH_TOKEN", "")
    monkeypatch.setattr(db, "DB_PATH", str(path))
    get_settings.cache_clear()
    db.reset_database()
    try:
        with TestClient(create_app()) as client:
            body = {"symbol": "TEST", "action": "BUY", "entry_price": 100,
                    "quantity": 100, "stop_loss": 99}
            assert client.post("/api/v1/positions/", json=body | {"quantity": 101}).status_code == 409
            first = client.post("/api/v1/positions/", json=body)
            assert first.status_code == 200, first.text
            second = client.post("/api/v1/positions/", json=body | {"symbol": "SECOND"})
            assert second.status_code == 200, second.text
            assert client.post(f"/api/v1/positions/{first.json()['id']}/close?exit_price=60").status_code == 200
            blocked = client.post("/api/v1/positions/", json=body | {"symbol": "THIRD"})
            assert blocked.status_code == 409 and "daily-loss" in blocked.text
            assert client.post(f"/api/v1/positions/{second.json()['id']}/close?exit_price=100").status_code == 200
    finally:
        db.reset_database()
        get_settings.cache_clear()
