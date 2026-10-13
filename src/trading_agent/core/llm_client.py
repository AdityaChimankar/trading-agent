"""LLM client for decision making."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from trading_agent.core.config import get_settings
from trading_agent.core.exceptions import ModelError
from trading_agent.core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class LLMRequest:
    """LLM request structure."""

    system_prompt: str
    user_prompt: str
    temperature: float = 0.1
    max_tokens: int = 1000
    response_format: dict | None = None


@dataclass(frozen=True)
class LLMResponse:
    """LLM response structure."""

    content: str
    model: str
    usage: dict[str, int] | None = None
    latency_ms: float = 0.0


class LLMClient(ABC):
    """Abstract LLM client."""

    @abstractmethod
    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Complete a prompt."""
        pass

    @abstractmethod
    async def close(self) -> None:
        """Close client connections."""
        pass


class OpenAICompatibleClient(LLMClient):
    """OpenAI-compatible API client (OpenAI, Azure, local models)."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 60.0,
        max_retries: int = 3,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout,
            )
        return self._client

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError)),
    )
    async def complete(self, request: LLMRequest) -> LLMResponse:
        client = await self._get_client()

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }

        if request.response_format:
            payload["response_format"] = request.response_format

        start = time.perf_counter()
        try:
            response = await client.post("/v1/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()

            latency = (time.perf_counter() - start) * 1000

            return LLMResponse(
                content=data["choices"][0]["message"]["content"],
                model=data.get("model", self.model),
                usage=data.get("usage"),
                latency_ms=latency,
            )
        except httpx.HTTPStatusError as e:
            logger.error("llm_api_error", status=e.response.status_code, error=e.response.text)
            raise ModelError(f"LLM API error: {e.response.status_code}") from e

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()


class AnthropicClient(LLMClient):
    """Anthropic Claude API client."""

    def __init__(
        self,
        api_key: str,
        model: str = "claude-3-haiku-20240307",
        timeout: float = 60.0,
    ):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url="https://api.anthropic.com",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                timeout=self.timeout,
            )
        return self._client

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError)),
    )
    async def complete(self, request: LLMRequest) -> LLMResponse:
        client = await self._get_client()

        payload = {
            "model": self.model,
            "system": request.system_prompt,
            "messages": [{"role": "user", "content": request.user_prompt}],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }

        start = time.perf_counter()
        try:
            response = await client.post("/v1/messages", json=payload)
            response.raise_for_status()
            data = response.json()

            latency = (time.perf_counter() - start) * 1000

            return LLMResponse(
                content=data["content"][0]["text"],
                model=self.model,
                usage={
                    "input_tokens": data.get("usage", {}).get("input_tokens", 0),
                    "output_tokens": data.get("usage", {}).get("output_tokens", 0),
                },
                latency_ms=latency,
            )
        except httpx.HTTPStatusError as e:
            logger.error(
                "anthropic_api_error", status=e.response.status_code, error=e.response.text
            )
            raise ModelError(f"Anthropic API error: {e.response.status_code}") from e

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()


class MockLLMClient(LLMClient):
    """Mock LLM client for testing."""

    def __init__(self, responses: dict[str, str] | None = None):
        self.responses = responses or {}
        self.call_count = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.call_count += 1
        key = request.user_prompt[:100]
        content = self.responses.get(
            key, '{"action": "HOLD", "confidence": 0.5, "reasoning": "Mock response"}'
        )
        return LLMResponse(
            content=content,
            model="mock",
            usage={"prompt_tokens": 100, "completion_tokens": 50},
            latency_ms=10.0,
        )

    async def close(self) -> None:
        pass


def create_llm_client(provider: str | None = None) -> LLMClient:
    """Create LLM client based on configuration."""
    get_settings()

    # Try to get provider from settings or environment
    if provider is None:
        provider = "openai"  # default

    if provider == "openai":
        # Use OpenAI-compatible (could be OpenAI, Azure, Ollama, etc.)
        base_url = "https://api.openai.com/v1"
        api_key = ""  # Would come from settings/env
        model = "gpt-4o-mini"
        return OpenAICompatibleClient(base_url, api_key, model)
    elif provider == "anthropic":
        api_key = ""  # Would come from settings/env
        return AnthropicClient(api_key)
    elif provider == "mock":
        return MockLLMClient()
    else:
        raise ValueError(f"Unknown LLM provider: {provider}")
