"""Higgsfield API client (https://docs.higgsfield.ai).

One async-job API in front of Soul (images), Kling, Seedance, PixVerse, Wan... (video):
  POST {base}/{endpoint-id}            -> {status: queued, request_id, status_url, cancel_url}
  GET  {base}/requests/{id}/status     -> {status: queued|in_progress|completed|failed|nsfw|canceled, ...}
  POST {base}/files/generate-upload-url -> {public_url, upload_url, upload_headers}
Auth: "Authorization: Key <id>:<secret>". Failed generations are not billed.
"""

from __future__ import annotations

import logging
import mimetypes
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from ..budget import BudgetGuard
from ..series import Character, Models, ModelSpec
from ..templating import extract, render
from ._http import retryable as _retryable

log = logging.getLogger(__name__)

TERMINAL_OK = {"completed"}
TERMINAL_BAD = {"failed", "nsfw", "canceled"}


class HiggsfieldError(RuntimeError):
    def __init__(self, message: str, status: str | None = None, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class HiggsfieldClient:
    def __init__(
        self,
        key_id: str,
        key_secret: str,
        base_url: str = "https://api.higgsfield.ai",
        http: httpx.Client | None = None,
        poll_interval: float = 5.0,
        timeout_s: float = 1200.0,
    ):
        if not key_id or not key_secret:
            raise ValueError("HF_API_KEY_ID and HF_API_KEY_SECRET are required")
        self.base_url = base_url.rstrip("/")
        self.poll_interval = poll_interval
        self.timeout_s = timeout_s
        self.http = http or httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0))
        self._auth = {"Authorization": f"Key {key_id}:{key_secret}"}

    @retry(
        retry=retry_if_exception(_retryable),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, max=30),
        reraise=True,
    )
    def _post(self, url: str, body: dict[str, Any], idem_key: str) -> dict[str, Any]:
        r = self.http.post(url, json=body, headers={**self._auth, "Idempotency-Key": idem_key})
        if 400 <= r.status_code < 500 and r.status_code != 429:
            # schema/key problems: surface the API's own explanation (wrong field names show up here),
            # and never retry. Rejected requests aren't billed.
            raise HiggsfieldError(
                f"{url.removeprefix(self.base_url)} rejected the request: HTTP {r.status_code} "
                f"{r.text[:600]}",
                str(r.status_code),
                r.text,
            )
        r.raise_for_status()
        return r.json()

    @retry(
        retry=retry_if_exception(_retryable),
        stop=stop_after_attempt(6),
        wait=wait_exponential(multiplier=2, max=30),
        reraise=True,
    )
    def _get(self, url: str) -> dict[str, Any]:
        r = self.http.get(url, headers=self._auth)
        r.raise_for_status()
        return r.json()

    def submit(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        # the same idempotency key is reused across retries so a timed-out submit isn't double-billed
        return self._post(f"{self.base_url}/{endpoint.strip('/')}", body, str(uuid.uuid4()))

    def wait(self, submitted: dict[str, Any]) -> dict[str, Any]:
        status_url = (
            submitted.get("status_url") or f"{self.base_url}/requests/{submitted['request_id']}/status"
        )
        deadline = time.monotonic() + self.timeout_s
        while True:
            data = self._get(status_url)
            status = data.get("status")
            if status in TERMINAL_OK:
                return data
            if status in TERMINAL_BAD:
                raise HiggsfieldError(f"request {data.get('request_id')} ended as '{status}'", status, data)
            if time.monotonic() > deadline:
                raise HiggsfieldError(
                    f"request timed out after {self.timeout_s:.0f}s (last: {status})", status
                )
            time.sleep(self.poll_interval)

    def run(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        submitted = self.submit(endpoint, body)
        log.info("higgsfield %s -> %s", endpoint, submitted.get("request_id"))
        return self.wait(submitted)

    def upload_file(self, path: Path) -> str:
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        meta = self._post(
            f"{self.base_url}/files/generate-upload-url", {"content_type": content_type}, str(uuid.uuid4())
        )
        headers = meta.get("upload_headers") or {"Content-Type": content_type}
        # presigned storage URL: never send Higgsfield credentials here
        r = self.http.put(meta["upload_url"], content=path.read_bytes(), headers=headers)
        r.raise_for_status()
        return meta["public_url"]


class HiggsfieldMedia:
    """Runs the image/video ModelSpecs from the series YAML through Higgsfield, under the budget guard."""

    provider_name = "higgsfield"

    def __init__(self, client: HiggsfieldClient, models: Models, guard: BudgetGuard, base_dir: Path):
        self.client = client
        self.models = models
        self.guard = guard
        self.base_dir = base_dir
        self._ref_cache: dict[str, tuple[float, str]] = {}

    def _call(
        self, spec: ModelSpec, ctx: dict[str, Any], seconds: float, job_id: int | None, label: str
    ) -> str:
        cost = spec.estimate(seconds)
        self.guard.check(cost, job_id)
        result = self.client.run(spec.endpoint, render(spec.input, ctx))
        url = extract(result, spec.output_path)
        self.guard.record(self.provider_name, f"{label} {spec.endpoint}", cost, job_id)
        return url

    def image(self, ctx: dict[str, Any], job_id: int | None) -> str:
        return self._call(self.models.image, ctx, 0, job_id, "image")

    def video(self, ctx: dict[str, Any], job_id: int | None) -> str:
        return self._call(self.models.video, ctx, float(ctx["duration"]), job_id, "video")

    def take(self, name: str, ctx: dict[str, Any], job_id: int | None) -> str:
        """v3: the whole reel in one call, on the named model from models.takes."""
        return self._call(self.models.takes[name], ctx, float(ctx["duration"]), job_id, f"take:{name}")

    def reference_urls(self, characters: list[Character]) -> list[str]:
        """Upload character reference images (cached for 30 min per process)."""
        urls = []
        for ch in characters:
            for p in ch.reference_images:
                path = p if p.is_absolute() else self.base_dir / p
                key = f"{path}:{path.stat().st_mtime}"
                hit = self._ref_cache.get(key)
                if hit and time.monotonic() - hit[0] < 1800:
                    urls.append(hit[1])
                    continue
                url = self.client.upload_file(path)
                self._ref_cache[key] = (time.monotonic(), url)
                urls.append(url)
        return urls
