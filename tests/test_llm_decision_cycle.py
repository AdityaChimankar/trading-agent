"""The scheduled LLM cycle stores only real decisions and stops on a failing provider."""
import asyncio
from types import SimpleNamespace

import pandas as pd

from trading_agent.strategy import llm_decision_agent as module


def _run(monkeypatch, outcomes, symbols):
    asked, stored = [], []

    class Agent:
        def __init__(self, use_mock=False):
            pass

        async def decide(self, candidate):
            asked.append(candidate.symbol)
            action = outcomes[len(asked) - 1]
            return module.LLMDecision(action=action or "HOLD", confidence=0.6 if action else 0.0,
                                      reasoning="ok" if action else "LLM error: 404",
                                      key_factors=[], risk_assessment="LOW", failed=action is None)

        async def close(self):
            pass

    async def store(symbol, decision, interval):
        stored.append((symbol, decision.action))

    monkeypatch.setattr(module, "LLMDecisionAgent", Agent)
    monkeypatch.setattr(module, "_store_llm_signal", store)
    monkeypatch.setattr("trading_agent.strategy.candidates.CandidateGenerator",
                        lambda: SimpleNamespace(generate_from_data=lambda df, symbol, interval: SimpleNamespace(symbol=symbol)))
    monkeypatch.setattr("trading_agent.storage.parquet_store.get_parquet_store",
                        lambda: SimpleNamespace(read_latest=lambda symbol, interval, n: pd.DataFrame({"close": [1.0]})))
    asyncio.run(module.run_llm_decision_cycle(symbols, interval="5minute"))
    return asked, stored


def test_failed_calls_are_not_stored_as_holds(monkeypatch):
    asked, stored = _run(monkeypatch, ["BUY", None, "SELL", None, "HOLD"], ["A", "B", "C", "D", "E"])
    assert asked == ["A", "B", "C", "D", "E"]
    assert stored == [("A", "BUY"), ("C", "SELL"), ("E", "HOLD")]


def test_a_provider_failing_every_call_stops_the_cycle(monkeypatch):
    asked, stored = _run(monkeypatch, [None] * 10, [f"S{i}" for i in range(10)])
    assert asked == ["S0", "S1", "S2"] and stored == []
