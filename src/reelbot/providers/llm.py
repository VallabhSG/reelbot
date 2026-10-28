"""Minimal OpenAI-compatible chat client (Groq, OpenRouter, OpenAI, Ollama...) with JSON output."""

from __future__ import annotations

import json
from typing import Any

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from ._http import retryable


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str, http: httpx.Client | None = None):
        if not api_key:
            raise ValueError("LLM_API_KEY is required")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.http = http or httpx.Client(timeout=120.0)
        self._headers = {"Authorization": f"Bearer {api_key}"}

    @retry(
        retry=retry_if_exception(retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, max=20),
        reraise=True,
    )
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
