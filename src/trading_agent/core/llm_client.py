"""LLM client for decision making."""

from __future__ import annotations

import asyncio
import os
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

OPENROUTER_BASE_URL = "https://openrouter.ai/api"


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


class _RateLimiter:
    """Process-wide minimum spacing between LLM calls (shared by all clients)."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def wait(self, min_interval: float) -> None:
        if min_interval <= 0:
            return
        async with self._lock:
            delay = self._next_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_at = time.monotonic() + min_interval

    def defer(self, seconds: float) -> None:
        """Push every caller back after the provider told us to slow down."""
        self._next_at = max(self._next_at, time.monotonic() + seconds)


_rate_limiter = _RateLimiter()
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _retry_after_seconds(response: httpx.Response) -> float | None:
    try:
        return min(float(response.headers["retry-after"]), 60.0)
    except (KeyError, ValueError):
        return None


class _RetryableStatus(Exception):
    def __init__(self, status: int, retry_after: float | None):
        super().__init__(status)
        self.status = status
        self.retry_after = retry_after


class OpenAICompatibleClient(LLMClient):
    """OpenAI-compatible API client (OpenRouter, OpenAI, Azure, local models)."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 60.0,
        max_retries: int = 4,
        min_interval_seconds: float = 0.0,
        extra_headers: dict[str, str] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.min_interval_seconds = min_interval_seconds
        self.extra_headers = extra_headers or {}
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {self.api_key}", **self.extra_headers},
                timeout=self.timeout,
            )
        return self._client

    async def complete(self, request: LLMRequest) -> LLMResponse:
        for attempt in range(self.max_retries + 1):
            await _rate_limiter.wait(self.min_interval_seconds)
            try:
                return await self._complete_once(request)
            except _RetryableStatus as e:
                if attempt >= self.max_retries:
                    raise ModelError(f"LLM API error: {e.status} after {attempt + 1} attempts") from e
                wait = e.retry_after if e.retry_after is not None else min(2.0 ** (attempt + 1), 60.0)
                logger.warning("llm_retrying", status=e.status, attempt=attempt + 1, wait_s=wait)
                _rate_limiter.defer(wait)
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                if attempt >= self.max_retries:
                    raise ModelError(f"LLM API unreachable: {e}") from e
                _rate_limiter.defer(min(2.0 ** (attempt + 1), 30.0))
        raise AssertionError("unreachable")

    async def _complete_once(self, request: LLMRequest) -> LLMResponse:
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
            status = e.response.status_code
            if status in _RETRYABLE_STATUS:
                raise _RetryableStatus(status, _retry_after_seconds(e.response)) from e
            logger.error("llm_api_error", status=status, error=e.response.text)
            raise ModelError(f"LLM API error: {status}") from e

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
    settings = get_settings()

    if provider is None:
        provider = settings.llm.provider

    if provider == "mock":
        return MockLLMClient()

    api_key = settings.llm.api_key
    if provider == "openrouter" and not api_key:
        api_key = os.getenv("OPENROUTER_API_KEY", "")

    if not api_key:
        logger.warning("llm_api_key_missing_using_mock", provider=provider)
        return MockLLMClient()

    if provider == "openrouter":
        base_url = settings.llm.base_url
        if "openai.com" in base_url:
            base_url = OPENROUTER_BASE_URL
        return OpenAICompatibleClient(
            base_url,
            api_key,
            settings.llm.model,
            max_retries=settings.llm.max_retries,
            min_interval_seconds=settings.llm.min_interval_seconds,
            extra_headers={"X-Title": "trading-agent"},
        )
    if provider == "openai":
        return OpenAICompatibleClient(
            settings.llm.base_url,
            settings.llm.api_key,
            settings.llm.model,
        )
    elif provider == "anthropic":
        return AnthropicClient(settings.llm.api_key, settings.llm.model)
    else:
        raise ValueError(f"Unknown LLM provider: {provider}")
