"""Provider contracts with mocked transport; never spend tokens or fetch news."""
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from trading_agent.analysis import research_news as news


@pytest.fixture
def providers(monkeypatch, tmp_path):
    settings = SimpleNamespace(data_dir=tmp_path, llm=SimpleNamespace(provider="mock"))
    monkeypatch.setattr(news, "get_settings", lambda: settings)
    response = SimpleNamespace(content=b"fixture", raise_for_status=lambda: None)
    http = AsyncMock()
    http.__aenter__.return_value = http
    http.get.return_value = response
    monkeypatch.setattr(news.httpx, "AsyncClient", lambda **kwargs: http)
    now = datetime.now(UTC) - timedelta(minutes=1)
    entries = [{"title": f"Headline {i}", "link": f"https://example.test/{i}",
                "published_parsed": now.timetuple(), "source": {"title": "Fixture"}}
               for i in range(20)]
    monkeypatch.setattr(news.feedparser, "parse", lambda content: SimpleNamespace(entries=entries))
    llm = AsyncMock()
    llm.complete.return_value = SimpleNamespace(
        content=json.dumps({"score": .4, "confidence": .8, "rationale": "Fixture only"}), model="test-only")
    monkeypatch.setattr(news, "create_llm_client", lambda: llm)
    return settings, http, llm, entries


async def test_headlines_are_bounded_and_no_llm_without_opt_in(providers):
    _, http, llm, _ = providers
    result = await news.refresh_news("RELIANCE")
    assert len(result["articles"]) == 10
    assert result["status"] == "headlines_only" and result["analysis"] is None
    http.get.assert_awaited_once()
    llm.complete.assert_not_awaited()
    assert news.read_news("RELIANCE") == result


async def test_mock_provider_never_fabricates_sentiment(providers):
    _, _, llm, _ = providers
    result = await news.refresh_news("TEST", True)
    assert result["status"] == "llm_not_configured" and result["analysis"] is None
    llm.complete.assert_not_awaited()


async def test_configured_provider_opt_in_validates_response_and_closes(providers):
    settings, _, llm, _ = providers
    settings.llm.provider = "openai"
    result = await news.refresh_news("TEST", True)
    assert result["status"] == "analyzed"
    assert result["analysis"]["score"] == .4
    assert result["analysis"]["model"] == "test-only"
    llm.complete.assert_awaited_once()
    llm.close.assert_awaited_once()
    request = llm.complete.call_args.args[0]
    assert "untrusted" in request.system_prompt
    assert len(json.loads(request.user_prompt)["headlines"]) == 10


async def test_invalid_llm_response_does_not_overwrite_cache(providers):
    settings, _, llm, _ = providers
    previous = await news.refresh_news("TEST")
    settings.llm.provider = "openai"
    llm.complete.return_value.content = '{"score": 100, "confidence": 1, "rationale": "invalid"}'
    with pytest.raises(ValueError):
        await news.refresh_news("TEST", True)
    llm.close.assert_awaited_once()
    assert news.read_news("TEST") == previous


async def test_old_future_and_undated_headlines_are_not_scored(providers):
    settings, _, llm, entries = providers
    settings.llm.provider = "openai"
    entries[:] = [{"published_parsed": (datetime.now(UTC) - timedelta(days=3)).timetuple()},
                  {"published_parsed": (datetime.now(UTC) + timedelta(days=1)).timetuple()}, {}]
    result = await news.refresh_news("TEST", True)
    assert result["articles"] == [] and result["analysis"] is None
    llm.complete.assert_not_awaited()


async def test_invalid_symbol_rejected_before_network(providers):
    _, http, _, _ = providers
    with pytest.raises(ValueError):
        await news.refresh_news("../private", True)
    http.get.assert_not_awaited()
