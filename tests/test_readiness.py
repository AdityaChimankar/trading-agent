from datetime import UTC, datetime, timedelta
from threading import RLock
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from trading_agent.analysis import readiness as module
from trading_agent.api.routers import research

NOW = datetime(2026, 10, 20, 5, tzinfo=UTC)


@pytest.fixture
def inputs(monkeypatch):
    scan = {"status": "completed", "rows": [
        {"symbol": "AAA", "interval": "minute", "timestamp": (NOW-timedelta(minutes=1)).isoformat(),
         "plan": {"blocked": False, "quantity": 10}}],
        "options": {"interval": "minute", "mode": "rule_only", "reward_risk": 2},
        "errors": [], "skipped": [{"symbol": "BBB", "reason": "insufficient_history"}],
        "processed": 2, "total": 2, "ledger_fingerprint": "same",
        "completed_at": NOW.isoformat()}
    ledger = {"fingerprint": "same", "entries_allowed": True, "block_reason": None,
              "realized_equity": 100000, "daily_realized_pnl": 0, "realized_drawdown_pct": 0,
              "accounting_basis": "realized_gross_entry_cost"}
    monkeypatch.setattr(module, "universe", lambda: ["AAA", "BBB"])
    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(api=SimpleNamespace(workers=1)))
    monkeypatch.setattr(module, "get_parquet_store", lambda: SimpleNamespace(
        get_symbols=lambda: ["AAA"], get_intervals=lambda symbol: ["minute"]))
    monkeypatch.setattr(module, "storage_coverage", lambda store, symbols, interval: {
        "status": "ready", "observed_at": NOW.isoformat(),
        "stored_for_interval": sum(interval in store.get_intervals(s) for s in set(store.get_symbols()) & set(symbols))})
    monkeypatch.setattr(module, "get_research_jobs", lambda: SimpleNamespace(lock=RLock(), state=scan))
    monkeypatch.setattr(module, "read_ledger_state", lambda *args: ledger)
    monkeypatch.setattr(module, "candidate_status", lambda: {
        "active": None, "error": None, "candidates": [{"status": "historical_gate_failed"}]})
    monkeypatch.setattr(module, "ingest_heartbeat", lambda: {"connected": True, "heartbeat_age_sec": 1000})
    monkeypatch.setattr(module, "estimate", lambda *args: {"status": "qualified_research", "expected_net_return_pct": .1})
    return scan, ledger


def test_readiness_distinguishes_stored_fresh_and_qualified(inputs):
    report = module.readiness("minute", NOW)
    assert report["coverage"]["stored_for_interval"] == 1
    assert report["coverage"]["configured"] == 2
    assert report["coverage"]["full_universe"]
    assert report["coverage"]["eligible"] == 1
    assert not report["feed"]["connected"]
    assert report["model"]["latest_candidate_status"] == "historical_gate_failed"
    assert not report["live_trading_ready"]
    assert report["execution_mode"] == "paper_only"
    assert report["status"] == "attention"
    assert report["coverage"]["scan_interval"] == "minute"
    other = module.readiness("5minute", NOW)
    assert other["coverage"]["eligible"] == 0
    assert other["coverage"]["stored_for_interval"] == 0


def test_readiness_blocks_changed_ledger_failed_evidence_and_incomplete_scan(inputs, monkeypatch):
    scan, ledger = inputs
    ledger["fingerprint"] = "changed"
    assert module.readiness("minute", NOW)["coverage"]["eligible"] == 0
    ledger["fingerprint"] = "same"
    monkeypatch.setattr(module, "estimate", lambda *args: {"status": "unqualified"})
    assert module.readiness("minute", NOW)["coverage"]["eligible"] == 0
    scan["status"] = "cancelled"
    assert not module.readiness("minute", NOW)["coverage"]["full_universe"]


def test_failed_storage_and_ledger_never_become_ready(inputs, monkeypatch):
    def failed(*args):
        raise OSError("internal details not exposed")
    monkeypatch.setattr(module, "get_parquet_store", failed)
    monkeypatch.setattr(module, "read_ledger_state", failed)
    report = module.readiness("minute", NOW)
    assert report["paper"]["entries_allowed"] is None
    assert report["coverage"]["eligible"] == 0
    assert {c["code"] for c in report["checks"]} >= {"storage_unavailable", "ledger_unavailable"}
    assert "internal details" not in str(report)


def test_readiness_route_validates_interval(monkeypatch, inputs):
    app = FastAPI()
    app.include_router(research.router)
    with TestClient(app) as client:
        assert client.get("/readiness?interval=minute").status_code == 200
        assert client.get("/readiness?interval=invalid").status_code == 422


def test_unreadable_outcome_evidence_never_counts_as_eligible(inputs, monkeypatch):
    def failed(*args):
        raise OSError("private artifact path")
    monkeypatch.setattr(module, "estimate", failed)
    report = module.readiness("minute", NOW)
    assert report["coverage"]["eligible"] == 0
    assert any(check["code"] == "evidence_unavailable" for check in report["checks"])
    assert "private artifact path" not in str(report)


def test_multiple_workers_never_report_eligible_allocations(inputs, monkeypatch):
    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(api=SimpleNamespace(workers=2)))
    report = module.readiness("minute", NOW)
    assert report["coverage"]["eligible"] == 0
    assert any(check["code"] == "unsupported_workers" for check in report["checks"])


@pytest.mark.parametrize("status,code", [("pending", "storage_pending"), ("error", "storage_unavailable")])
def test_pending_inventory_never_fabricates_missing_directory_counts(inputs, monkeypatch, status, code):
    monkeypatch.setattr(module, "storage_coverage", lambda *args: {
        "status": status, "stored_for_interval": None, "observed_at": None})
    report = module.readiness("minute", NOW)
    assert report["coverage"]["configured"] == 2
    assert report["coverage"]["stored_for_interval"] is None
    assert report["coverage"]["missing_for_interval"] is None
    assert report["coverage"]["storage_status"] == status
    assert any(c["code"] == code for c in report["checks"])
