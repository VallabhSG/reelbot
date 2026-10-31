"""Minimal OpenAI-compatible chat client (Groq, OpenRouter, OpenAI, Ollama...) with JSON output."""

from __future__ import annotations

import json
from typing import Any

import httpx
from tenacity import RetryCallState, retry, retry_if_exception, stop_after_attempt

from ._http import retryable

RATE_LIMIT_MAX_WAIT = 65.0  # free tiers count tokens per minute: one minute always clears it


class AnswerCutOff(ValueError):
    """The model's answer stopped before its JSON was complete (its output budget ran out, which
    on reasoning models includes the hidden thinking). Asking again, or for less at once, helps;
    retrying the same request blindly rarely does."""


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
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        http: httpx.Client | None = None,
        reasoning_effort: str = "",
    ):
        if not api_key:
            raise ValueError("LLM_API_KEY is required")
        self.base_url = base_url.rstrip("/")
        self.model = model
        # gpt-oss thinks before it answers, and the thinking comes out of the same output budget
        # as the JSON: on Groq a long plan got cut off (31 Oct). "low" leaves room for the answer.
        self.reasoning_effort = reasoning_effort if "gpt-oss" in model else ""
        self.http = http or httpx.Client(timeout=120.0)
        self._headers = {"Authorization": f"Bearer {api_key}"}

    @retry(retry=retry_if_exception(retryable), stop=stop_after_attempt(6), wait=_wait, reraise=True)
    def json_chat(self, system: str, user: str, temperature: float = 0.9) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        r = self.http.post(f"{self.base_url}/chat/completions", headers=self._headers, json=body)
        if r.status_code >= 400:
            err = _error(r)
            if err.get("code") == "json_validate_failed":  # Groq: the JSON came out broken or cut
                partial = str(err.get("failed_generation") or "")
                raise AnswerCutOff(f"the model's JSON was incomplete ({len(partial)} characters came back)")
            detail = str(err.get("message") or "")[:300]
            raise httpx.HTTPStatusError(
                f"{r.status_code} from {self.base_url}" + (f": {detail}" if detail else ""),
                request=r.request,
                response=r,
            )
        choice = r.json()["choices"][0]
        content = choice["message"].get("content") or ""
        if choice.get("finish_reason") == "length":
            raise AnswerCutOff(f"the answer hit the output limit after {len(content)} characters")
        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            raise AnswerCutOff(f"the answer was not complete JSON ({len(content)} characters)") from e


def _error(r: httpx.Response) -> dict[str, Any]:
    """The provider's error object, if the body has one ({"error": {"message", "code", ...}})."""
    try:
        err = r.json().get("error")
    except ValueError:
        return {}
    return err if isinstance(err, dict) else ({"message": err} if err else {})
