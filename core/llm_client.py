"""
Shared OpenRouter client for every LLM call in the project.

Single place that knows the OpenRouter base URL, the free model id, and
the JSON-extraction rules, so analysis/, strategy/ and scripts/ all speak
to the same endpoint with the same defaults.

Free-tier notes:
  - Models suffixed `:free` are rate-limited per day/minute by OpenRouter.
  - The `openrouter/free` router automatically picks a working free model.
  - If one model goes down, it falls back to the next in the list.
  - See https://openrouter.ai/models?max_price=0 for the current list.
"""
import json
import os
import re
import time
from functools import lru_cache
from typing import Optional

from openai import OpenAI
from openai import APIError, RateLimitError, APIConnectionError

BASE_URL = "https://openrouter.ai/api/v1"

# OpenRouter's auto free router - automatically picks a working free model
AUTO_FREE_ROUTER = "openrouter/free"

# Fallback models for sentiment scoring (ordered by capability)
# These are known good free models for text analysis/sentiment
SENTIMENT_FALLBACK_MODELS = [
    "google/gemma-4-26b-a4b-it:free",      # Google Gemma 4 - strong at text analysis
    "google/gemma-4-31b-it:free",          # Larger Gemma variant
    "nvidia/nemotron-3-ultra-550b-a55b:free",  # NVIDIA Nemotron 3 Ultra - very capable
    "nvidia/nemotron-3.5-lightning:free",  # Fast Nemotron variant
    "qwen/qwen3.8-27b:free",               # Qwen - good multilingual support
    "thinkingmachines/inkling:free",       # Thinking Machines Inkling
    "poolside/laguna-s-2.1:free",          # Poolside Laguna
    "cohere/north-mini-code:free",         # Cohere North Mini
]

# Default model - use auto router first, then fallbacks
DEFAULT_FREE_MODEL = AUTO_FREE_ROUTER


def get_model() -> str:
    """Get the configured model from env, defaulting to auto free router."""
    return os.getenv("OPENROUTER_MODEL", DEFAULT_FREE_MODEL)


@lru_cache(maxsize=1)
def get_client() -> OpenAI:
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set - add it to .env")
    return OpenAI(api_key=api_key, base_url=BASE_URL)


def _try_complete(client: OpenAI, model: str, prompt: str, temperature: float) -> Optional[str]:
    """Try to complete with a specific model, return None on failure."""
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            timeout=30.0,
        )
        content = response.choices[0].message.content
        if content:
            return content.strip()
    except (APIError, RateLimitError, APIConnectionError, Exception):
        pass
    return None


def complete(prompt: str, temperature: float = 0.0, task_type: str = "general") -> str:
    """
    Send a single-turn prompt to the configured OpenRouter model with automatic fallback.
    
    Args:
        prompt: The prompt to send
        temperature: Sampling temperature (0.0 for deterministic)
        task_type: "sentiment" for sentiment scoring, "general" for other tasks
    
    Returns:
        The model's response text
    
    Raises:
        RuntimeError: If all models fail
    """
    client = get_client()
    primary_model = get_model()
    
    # Build model list: primary first, then task-specific fallbacks
    models_to_try = [primary_model]
    
    if task_type == "sentiment":
        # Add sentiment-optimized fallbacks (avoid duplicates)
        for m in SENTIMENT_FALLBACK_MODELS:
            if m not in models_to_try:
                models_to_try.append(m)
    else:
        # For general tasks, add a few reliable fallbacks
        general_fallbacks = [
            "nvidia/nemotron-3.5-lightning:free",
            "google/gemma-4-26b-a4b-it:free",
            "qwen/qwen3.8-27b:free",
        ]
        for m in general_fallbacks:
            if m not in models_to_try:
                models_to_try.append(m)
    
    last_error = None
    for i, model in enumerate(models_to_try):
        content = _try_complete(client, model, prompt, temperature)
        if content is not None:
            if i > 0:
                print(f"[LLM] Fallback to {model} succeeded (primary {primary_model} failed)")
            return content
        last_error = f"Model {model} failed"
        if i < len(models_to_try) - 1:
            time.sleep(0.5 * (i + 1))  # Brief backoff between retries
    
    raise RuntimeError(f"All models failed. Last error: {last_error}")


def extract_json(text: str) -> dict:
    """Pull a JSON object out of a model response.

    Models wrap JSON in ```json fences or add a sentence of preamble
    despite instructions, so fall back to scanning for the outermost
    brace-delimited block before giving up.
    """
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"No JSON object found in model output: {text[:200]}")
    return json.loads(text[start:end + 1])


def get_available_models() -> list[str]:
    """Return list of models to try for the current task type."""
    primary = get_model()
    models = [primary]
    for m in SENTIMENT_FALLBACK_MODELS:
        if m not in models:
            models.append(m)
    return models