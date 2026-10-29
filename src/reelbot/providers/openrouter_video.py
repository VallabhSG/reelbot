"""OpenRouter video generation (https://openrouter.ai/docs/guides/overview/multimodal/video-generation).

Same models as elsewhere at the model maker's own price (Seedance 2.0 720p: $7 per 1M video tokens,
~$0.15/s, about half Higgsfield's rate). One async-job API:
  POST {base}/videos                    {model, prompt, duration, resolution, aspect_ratio,
                                         generate_audio, frame_images: [{.., frame_type: first_frame}]}
                                        -> 202 {id, polling_url, status: pending}
  GET  {base}/videos/{id}               -> {status: pending|in_progress|completed|failed|..., usage.cost}
  GET  {base}/videos/{id}/content?index=0   the file (needs the API key)
Auth: "Authorization: Bearer <key>". A request is never re-submitted after it may have been
accepted: a duplicate job is a second charge.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from ._http import retryable

log = logging.getLogger(__name__)

BASE_URL = "https://openrouter.ai/api/v1"
TERMINAL_BAD = {"failed", "cancelled", "canceled", "expired"}


class OpenRouterError(RuntimeError):
    def __init__(self, message: str, status: str | None = None, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


def _submit_retryable(exc: BaseException) -> bool:
    """Retry a submit only when the request surely never started a job: it never connected, or
    OpenRouter answered 429/502/503. A read timeout after sending is NOT retried (maybe billed)."""
    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {429, 502, 503}
    return False


class OpenRouterVideo:
    def __init__(
        self,
        api_key: str,
        base_url: str = BASE_URL,
        http: httpx.Client | None = None,
        poll_interval: float = 20.0,
        timeout_s: float = 2400.0,
        work_dir: Path | None = None,
    ):
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY is required (a model in the series file uses OpenRouter)")
        self.base_url = base_url.rstrip("/")
        self.poll_interval = poll_interval
        self.timeout_s = timeout_s
        self.work_dir = work_dir or Path("data") / "openrouter"
        self.http = http or httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0))
        self._auth = {"Authorization": f"Bearer {api_key}"}

    # ------------------------------------------------------------------ requests
    @retry(
        retry=retry_if_exception(_submit_retryable),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=3, max=60),
        reraise=True,
    )
    def submit(self, model: str, body: dict[str, Any]) -> dict[str, Any]:
        r = self.http.post(f"{self.base_url}/videos", json={**body, "model": model}, headers=self._auth)
        if 400 <= r.status_code < 500 and r.status_code != 429:
            # bad key, unsupported duration/size, no credits...: OpenRouter says which; never retry
            raise OpenRouterError(
                f"OpenRouter rejected the {model} request: HTTP {r.status_code} {r.text[:600]}",
                str(r.status_code),
                r.text,
            )
        r.raise_for_status()
        return r.json()

    @retry(
        retry=retry_if_exception(retryable),
        stop=stop_after_attempt(6),
        wait=wait_exponential(multiplier=2, max=30),
        reraise=True,
    )
    def status(self, job_id: str) -> dict[str, Any]:
        r = self.http.get(f"{self.base_url}/videos/{job_id}", headers=self._auth)
        r.raise_for_status()
        return r.json()

    def wait(self, job_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout_s
        while True:
            data = self.status(job_id)
            state = str(data.get("status") or "")
            if state == "completed":
                return data
            if state in TERMINAL_BAD:
                why = data.get("error") or {k: v for k, v in data.items() if k not in {"id", "status"} and v}
                raise OpenRouterError(
                    f"OpenRouter job {job_id} ended as '{state}': "
                    f"{json.dumps(why, ensure_ascii=False)[:400] if why else '(no reason given)'}",
                    state,
                    data,
                )
            if time.monotonic() > deadline:
                raise OpenRouterError(
                    f"OpenRouter job {job_id} still '{state}' after {self.timeout_s:.0f}s; it may still "
                    "finish (and be billed) on OpenRouter's side",
                    state,
                    data,
                )
            time.sleep(self.poll_interval)

    @retry(
        retry=retry_if_exception(retryable),
        stop=stop_after_attempt(4),
        wait=wait_exponential(2, max=30),
        reraise=True,
    )
    def download(self, job_id: str, done: dict[str, Any], dest: Path) -> Path:
        """Signed `urls` need no key; otherwise the content endpoint, with the key, on OpenRouter's
        own host only (the key is never sent anywhere else)."""
        signed = [u for u in done.get("urls") or [] if isinstance(u, str)]
        if signed:
            url, headers = signed[0], {}
        else:
            url, headers = f"{self.base_url}/videos/{job_id}/content?index=0", self._auth
            unsigned = [u for u in done.get("unsigned_urls") or [] if isinstance(u, str)]
            if unsigned and urlparse(unsigned[0]).netloc == urlparse(self.base_url).netloc:
                url = unsigned[0]
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        with self.http.stream("GET", url, headers=headers, follow_redirects=True) as r:
            r.raise_for_status()
            with tmp.open("wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
        tmp.replace(dest)
        return dest

    # ------------------------------------------------------------------ one call
    def generate(self, model: str, body: dict[str, Any]) -> tuple[str, float | None]:
        """Submit, wait, download. Returns (file:// URL of the local copy, actual cost or None)."""
        job = self.submit(model, body)
        job_id = str(job["id"])
        log.info("openrouter %s -> job %s", model, job_id)
        done = self.wait(job_id)
        path = self.download(job_id, done, self.work_dir / f"{job_id}.mp4")
        cost = (done.get("usage") or {}).get("cost")
        return path.resolve().as_uri(), float(cost) if isinstance(cost, int | float) else None

    def key_info(self) -> dict[str, Any]:
        r = self.http.get(f"{self.base_url}/key", headers=self._auth)
        if r.status_code in {401, 403}:
            raise OpenRouterError(f"OpenRouter refused the key: HTTP {r.status_code} {r.text[:200]}")
        r.raise_for_status()
        return r.json().get("data") or {}

    def video_model(self, model: str) -> dict[str, Any]:
        r = self.http.get(f"{self.base_url}/videos/models", headers=self._auth)
        r.raise_for_status()
        for m in r.json().get("data") or []:
            if model in {m.get("id"), m.get("canonical_slug")}:
                return m
        raise OpenRouterError(f"OpenRouter has no video model '{model}'")
