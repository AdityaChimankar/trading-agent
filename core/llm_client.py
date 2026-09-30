"""
Shared OpenRouter client for every LLM call in the project.

Single place that knows the OpenRouter base URL, the free model id, and
the JSON-extraction rules, so analysis/, strategy/ and scripts/ all speak
to the same endpoint with the same defaults. Previously each module built
its own client, which meant changing the model or the endpoint meant
hunting through several files.

Free-tier notes:
  - Models suffixed `:free` are rate-limited per day/minute by OpenRouter.
    If one goes down, set OPENROUTER_MODEL in .env to another free id.
  - See https://openrouter.ai/models?max_price=0 for the current list.
"""
import json
import os
import re
from functools import lru_cache

from openai import OpenAI

BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_FREE_MODEL = "google/gemini-2.0-flash-exp:free"


def get_model() -> str:
    return os.getenv("OPENROUTER_MODEL", DEFAULT_FREE_MODEL)


@lru_cache(maxsize=1)
def get_client() -> OpenAI:
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set - add it to .env")
    return OpenAI(api_key=api_key, base_url=BASE_URL)


def complete(prompt: str, temperature: float = 0.0) -> str:
    """Send a single-turn prompt to the configured OpenRouter model."""
    client = get_client()
    response = client.chat.completions.create(
        model=get_model(),
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
    )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError(f"OpenRouter returned an empty response from {get_model()}")
    return content.strip()


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
