from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

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


def test_readonly_invalid_schema_does_not_initialize_tables(tmp_path, monkeypatch):
    path = tmp_path / "wrong-schema.db"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER)")
    monkeypatch.setattr(guard, "get_settings", lambda: SimpleNamespace(
        database=SimpleNamespace(url=f"sqlite:///{path.as_posix()}"), paper=PaperRiskSettings()))
    with pytest.raises(sqlite3.OperationalError):
        guard.read_ledger_state()
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("unrelated",)]


@pytest.mark.parametrize("change", [
    {"risk_amount": float("nan")}, {"risk_amount": 0}, {"position_value": 1},
    {"position_size": 1.5}, {"action": "HOLD"}, {"symbol": None},
])
def test_corrupt_open_positions_cannot_understate_exposure(change):
    position = {"id": 1, "symbol": "TEST", "action": "BUY", "status": "open",
                "entry_price": 100, "position_size": 10, "position_value": 1000,
                "stop_loss": 99, "risk_amount": 10}
    with pytest.raises((ValueError, TypeError)):
        guard.ledger_state([position | change], PaperRiskSettings(), NOW)


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


@pytest.fixture
def paper_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from trading_agent.api.main import create_app
    from trading_agent.core.config import get_settings
    from trading_agent.storage import db
    path = tmp_path / "ledger.db"
    monkeypatch.setenv("DB_URL", f"sqlite:///{path.as_posix()}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("STORAGE_PARQUET_ROOT", str(tmp_path / "parquet"))
    monkeypatch.setenv("ML_MODEL_DIR", str(tmp_path / "models"))
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
            yield client, path
    finally:
        db.reset_database()
        get_settings.cache_clear()


ENTRY = {"symbol": "TEST", "action": "BUY", "entry_price": 100,
         "quantity": 100, "stop_loss": 99}


def test_api_new_entries_blocked_but_exits_allowed_after_realized_loss(paper_client):
    client, _ = paper_client
    assert client.post("/api/v1/positions/", json=ENTRY | {"quantity": 101}).status_code == 409
    first = client.post("/api/v1/positions/", json=ENTRY)
    assert first.status_code == 200, first.text
    second = client.post("/api/v1/positions/", json=ENTRY | {"symbol": "SECOND"})
    assert second.status_code == 200, second.text
    assert client.post(f"/api/v1/positions/{first.json()['id']}/close?exit_price=60").status_code == 200
    blocked = client.post("/api/v1/positions/", json=ENTRY | {"symbol": "THIRD"})
    assert blocked.status_code == 409 and "daily-loss" in blocked.text
    assert client.post(f"/api/v1/positions/{second.json()['id']}/close?exit_price=100").status_code == 200


def test_api_invalid_closed_evidence_blocks_entries_but_preserves_exits(paper_client):
    client, path = paper_client
    first = client.post("/api/v1/positions/", json=ENTRY).json()
    second = client.post("/api/v1/positions/", json=ENTRY | {"symbol": "SECOND"}).json()
    client.post(f"/api/v1/positions/{first['id']}/close?exit_price=100")
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE open_positions SET pnl=NULL WHERE id=?", [first["id"]])
    blocked = client.post("/api/v1/positions/", json=ENTRY | {"symbol": "THIRD"})
    assert blocked.status_code == 503 and "cannot be verified" in blocked.text
    assert client.post(f"/api/v1/positions/{second['id']}/close?exit_price=100").status_code == 200


def test_api_realized_drawdown_remains_blocked_next_session(paper_client):
    client, path = paper_client
    short = ENTRY | {"action": "SELL", "stop_loss": 101}
    position = client.post("/api/v1/positions/", json=short).json()
    assert client.post(f"/api/v1/positions/{position['id']}/close?exit_price=260").status_code == 200
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE open_positions SET exit_time=? WHERE id=?",
                           [(datetime.now(UTC) - timedelta(days=1)).isoformat(), position["id"]])
    blocked = client.post("/api/v1/positions/", json=ENTRY)
    assert blocked.status_code == 409 and "drawdown" in blocked.text


def test_concurrent_clients_cannot_overbook_paper_positions(paper_client, monkeypatch):
    from trading_agent.core.config import get_settings
    client, _ = paper_client
    monkeypatch.setenv("PAPER_MAX_POSITIONS", "1")
    get_settings.cache_clear()
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda index: client.post(
            "/api/v1/positions/", json=ENTRY | {"symbol": f"CLIENT{index}"}), range(4)))
    assert sorted(response.status_code for response in responses) == [200, 409, 409, 409]
    assert len(client.get("/api/v1/positions/").json()) == 1


@pytest.mark.parametrize("failure", ["missing", "blocked", "legacy"])
def test_scan_snapshots_fail_closed_without_current_ledger_evidence(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(jobs, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    def read(*args):
        if failure == "missing":
            raise sqlite3.OperationalError("missing ledger")
        return {"fingerprint": "original", "entries_allowed": failure != "blocked",
                "block_reason": "Paper realized drawdown limit reached" if failure == "blocked" else None}
    monkeypatch.setattr(jobs, "read_ledger_state", read)
    job = jobs.ResearchJobs()
    job.state.update(rows=[{"symbol": "TEST", "validation": {"status": "unqualified"},
                           "plan": {"blocked": False, "quantity": 10, "position_value": 1000, "risk_amount": 20}}])
    if failure != "legacy":
        job.state["ledger_fingerprint"] = "original"
    result = job.snapshot()
    assert result["eligible"] == 0 and result["allocation_warning"]
    assert result["rows"][0]["plan"]["quantity"] == 0
    assert job.state["rows"][0]["plan"]["quantity"] == 10


@pytest.mark.parametrize("require_validated", [True, False])
@pytest.mark.parametrize("failure", [OSError, ValueError, RuntimeError, None])
def test_snapshot_unreadable_outcome_evidence_is_unavailable_and_gated(
    tmp_path, monkeypatch, require_validated, failure,
):
    monkeypatch.setattr(jobs, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(jobs, "read_ledger_state", lambda *args: {
        "fingerprint": "original", "entries_allowed": True, "block_reason": None})
    def estimate(*args):
        if failure:
            raise failure("private-path-or-secret must not appear in the response")
        return {"broken_evidence": True}
    monkeypatch.setattr(jobs, "estimate", estimate)
    job = jobs.ResearchJobs()
    job.state.update(ledger_fingerprint="original", options={"require_validated": require_validated},
                     rows=[{"symbol": "TEST", "validation": {"status": "qualified_research",
                             "win_probability": .8, "expected_net_return_pct": 2},
                            "plan": {"blocked": False, "quantity": 10, "position_value": 1000, "risk_amount": 20}}])
    result = job.snapshot()
    row = result["rows"][0]
    assert row["validation"] == {"status": "unavailable", "win_probability": None,
                                 "expected_net_return_pct": None,
                                 "reason": "Outcome evidence could not be verified"}
    assert row["plan"]["blocked"] is require_validated
    assert row["plan"]["quantity"] == (0 if require_validated else 10)
    assert result["eligible"] == (0 if require_validated else 1)
    assert "private-path-or-secret" not in str(result)
    assert job.state["rows"][0]["validation"]["status"] == "qualified_research"
    assert job.state["rows"][0]["plan"]["quantity"] == 10


@pytest.mark.parametrize("source", ["manual", "automation"])
def test_manual_and_automatic_scans_share_file_lock_without_mutating_options(tmp_path, monkeypatch, source):
    monkeypatch.setattr(jobs, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(jobs, "read_ledger_state", lambda *args: {
        "fingerprint": "original", "entries_allowed": True, "block_reason": None})
    monkeypatch.setattr(jobs.threading, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: None))
    options = {"symbols": ["TEST"], "capital": 100000}
    job = jobs.ResearchJobs()
    try:
        result = job.start(options) if source == "manual" else job.start(options, source=source)
        assert result["source"] == source
        assert options == {"symbols": ["TEST"], "capital": 100000}
        competing = jobs.ResearchJobs()
        with pytest.raises(RuntimeError, match="Another process"):
            competing.start(options, source="automation")
    finally:
        job.run_lock.release()


@pytest.mark.parametrize("blocked", [False, True])
def test_completed_scan_sizes_against_final_ledger_and_respects_loss_blocks(tmp_path, monkeypatch, blocked):
    limits = PaperRiskSettings(max_single_position_pct=.05)
    monkeypatch.setattr(jobs, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path, paper=limits))
    ledger = {"fingerprint": "original", "realized_equity": 200000, "positions": [],
              "entries_allowed": True, "block_reason": None}
    monkeypatch.setattr(jobs, "read_ledger_state", lambda *args: ledger)
    monkeypatch.setattr(jobs, "model_for_research", lambda: SimpleNamespace(status="loaded", warning=None))
    monkeypatch.setattr(jobs, "estimate", lambda *args: {"status": "unqualified"})
    def analyze(*args):
        ledger.update(fingerprint="changed-during-scan", realized_equity=100000,
                      entries_allowed=not blocked, block_reason="Paper realized daily-loss limit reached" if blocked else None)
        return {"symbol": "TEST", "action": "BUY", "score": 75, "stale": False,
                "price": 100, "atr": 2, "room_to_target": 12}, None
    monkeypatch.setattr(jobs, "analyze_symbol", analyze)
    job = jobs.ResearchJobs()
    job.run(["TEST"], {"capital": 200000, "interval": "minute", "mode": "rule_only",
                       "risk_pct": 1, "reward_risk": 2, "require_validated": False})
    result = job.snapshot()
    assert result["status"] == "completed"
    assert result["ledger_fingerprint"] == "changed-during-scan"
    assert result["effective_capital"] == 100000
    assert result["rows"][0]["plan"]["position_value"] == (0 if blocked else 5000)
    assert result["rows"][0]["plan"]["blocked"] is blocked
