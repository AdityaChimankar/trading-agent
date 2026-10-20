"""Offline contract checks for the research panels; no broker or AI calls."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from trading_agent.analysis.opportunity_finder import OpportunityConfig, OpportunityFinder
from trading_agent.api.routers import opportunities
from trading_agent.strategy.candidates import Candidate


@pytest.fixture
def candidate():
    return Candidate(
        symbol="TEST", timestamp=datetime(2026, 10, 19, 9, tzinfo=UTC),
        interval="minute", close=100, rsi=25, adx=30, atr=2,
        pattern_bias=1, rule_action="BUY", rule_confidence=0.9, volume=150000,
    )


def test_ranking_preserves_frozen_opportunities(candidate):
    generator = SimpleNamespace(generate_all=lambda *args: [
        replace(candidate, symbol="LOW", rule_confidence=0.5), candidate,
    ])
    finder = OpportunityFinder(
        config=OpportunityConfig(min_score=0), candidate_generator=generator,
        parquet_store=SimpleNamespace(),
    )
    ranked = finder.find_opportunities(["LOW", "TEST"], "minute")
    assert [item.symbol for item in ranked] == ["TEST", "LOW"]
    assert [item.rank for item in ranked] == [1, 2]
    assert ranked[0].ml_signal is None and ranked[0].llm_signal is None


@pytest.fixture
def client(monkeypatch, candidate):
    store = SimpleNamespace(get_symbols=lambda: ["TEST"])
    monkeypatch.setattr(opportunities, "get_parquet_store", lambda: store)
    monkeypatch.setattr(
        opportunities.CandidateGenerator, "generate_for_symbol",
        lambda self, symbol, interval, **kwargs: replace(candidate, interval=interval),
    )
    app = FastAPI()
    app.include_router(opportunities.router, prefix="/api/v1/opportunities")
    with TestClient(app) as instance:
        yield instance


def test_decision_preview_reports_missing_agents_and_timestamp(client):
    response = client.post("/api/v1/opportunities/test/decision?interval=minute")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["symbol"] == "TEST"
    assert result["interval"] == "minute"
    assert result["mode"] == "rule_only"
    assert result["timestamp"].startswith("2026-10-19")
    assert result["decision"]["action"] == "BUY"
    assert result["decision"]["ml_decision"] is None
    assert result["decision"]["llm_decision"] is None
    assert len(result["limitations"]) == 4


def test_decision_missing_symbol(client):
    assert client.post("/api/v1/opportunities/MISSING/decision").status_code == 404


def test_decision_insufficient_data(client, monkeypatch):
    monkeypatch.setattr(
        opportunities.CandidateGenerator, "generate_for_symbol", lambda *args: None,
    )
    assert client.post("/api/v1/opportunities/TEST/decision").status_code == 422


@pytest.mark.parametrize("path", ["/", "/TEST", "/TEST/decision"])
def test_rejects_invalid_interval(client, path):
    method = client.post if path.endswith("decision") else client.get
    assert method(f"/api/v1/opportunities{path}?interval=invalid").status_code == 422


def test_scan_uses_current_contract(client):
    result = client.get("/api/v1/opportunities/?symbols=TEST&interval=minute")
    assert result.status_code == 200, result.text
    assert result.json()[0]["rank"] == 1
    assert result.json()[0]["symbol"] == "TEST"


def test_details_use_requested_interval(candidate):
    import pandas as pd

    intervals = []

    def read_latest(symbol, interval, n):
        intervals.append(interval)
        return pd.DataFrame()

    finder = OpportunityFinder(
        candidate_generator=SimpleNamespace(generate_all=lambda *args: [candidate]),
        parquet_store=SimpleNamespace(read_latest=read_latest),
    )
    opportunity = finder.find_opportunities(["TEST"], "minute")[0]
    finder.get_opportunity_details(opportunity, "minute")
    assert intervals == ["minute"]
