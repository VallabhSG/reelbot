"""Minimal OpenAI-compatible chat client (Groq, OpenRouter, OpenAI, Ollama...) with JSON output."""

from __future__ import annotations

import json
from typing import Any

import httpx
from tenacity import RetryCallState, retry, retry_if_exception, stop_after_attempt

from ._http import retryable

RATE_LIMIT_MAX_WAIT = 65.0  # free tiers count tokens per minute: one minute always clears it


def _wait(state: RetryCallState) -> float:
    """429: wait as long as the provider asks (Retry-After), else back off exponentially."""
    exc = state.outcome.exception() if state.outcome else None
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        try:
            return min(float(exc.response.headers.get("retry-after", "")), RATE_LIMIT_MAX_WAIT) + 0.5
        except ValueError:
            return 20.0
    return min(2.0**state.attempt_number, 20.0)


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str, http: httpx.Client | None = None):
        if not api_key:
            raise ValueError("LLM_API_KEY is required")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.http = http or httpx.Client(timeout=120.0)
        self._headers = {"Authorization": f"Bearer {api_key}"}

    @retry(retry=retry_if_exception(retryable), stop=stop_after_attempt(6), wait=_wait, reraise=True)
    def json_chat(self, system: str, user: str, temperature: float = 0.9) -> dict[str, Any]:
        r = self.http.post(
            f"{self.base_url}/chat/completions",
            headers=self._headers,
            json={
                "model": self.model,
                "temperature": temperature,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            },
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        return json.loads(content)
