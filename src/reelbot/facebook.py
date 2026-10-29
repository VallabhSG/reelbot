"""Facebook Reels publishing to a Page (Graph API Reels Publishing API).

  1. POST graph.facebook.com/{ver}/{page-id}/video_reels  upload_phase=start      -> video_id
  2. POST rupload.facebook.com/video-upload/{ver}/{video_id}  (the file's bytes)
  3. POST graph.facebook.com/{ver}/{page-id}/video_reels  upload_phase=finish, video_state=PUBLISHED
  4. GET  graph.facebook.com/{ver}/{video_id}?fields=status  until published (or an error)

Needs a Page access token with pages_show_list, pages_read_engagement, pages_manage_posts. A Page
token made from a long-lived user token never expires (see the README). Limits: 9:16, 3-90 s,
30 API-published reels per Page per 24 h.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from .providers._http import retryable

log = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com"
RUPLOAD = "https://rupload.facebook.com/video-upload"
DESCRIPTION_MAX = 2200


class FacebookError(RuntimeError):
    pass


def _check(r: httpx.Response, what: str) -> dict[str, Any]:
    if r.status_code >= 400 and not retryable(httpx.HTTPStatusError("", request=r.request, response=r)):
        raise FacebookError(f"{what} -> {r.status_code}: {r.text[:500]}")
    r.raise_for_status()
    return r.json()


class FacebookPublisher:
    def __init__(
        self,
        page_id: str,
        page_token: str,
        version: str = "v26.0",
        http: httpx.Client | None = None,
        poll_interval: float = 10.0,
        poll_timeout: float = 600.0,
    ):
        if not (page_id and page_token):
            raise ValueError("FB_PAGE_ID and FB_PAGE_TOKEN are required")
        self.page_id = page_id
        self.token = page_token
        self.version = version.strip("/")
        self.http = http or httpx.Client(timeout=httpx.Timeout(300.0, connect=15.0))
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout

    @retry(
        retry=retry_if_exception(retryable),
        stop=stop_after_attempt(4),
        wait=wait_exponential(3, max=60),
        reraise=True,
    )
    def _graph(self, method: str, path: str, **params: Any) -> dict[str, Any]:
        url = f"{GRAPH}/{self.version}/{path}"
        if method == "POST":
            r = self.http.post(url, data={**params, "access_token": self.token})
        else:
            r = self.http.get(url, params={**params, "access_token": self.token})
        return _check(r, f"{method} {path}")

    def page_name(self) -> str:
        return self._graph("GET", self.page_id, fields="name").get("name", "?")

    @retry(
        retry=retry_if_exception(retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(5, max=60),
        reraise=True,
    )
    def _upload(self, video_id: str, path: Path) -> None:
        data = path.read_bytes()
        r = self.http.post(
            f"{RUPLOAD}/{self.version}/{video_id}",
            headers={
                "Authorization": f"OAuth {self.token}",
                "offset": "0",
                "file_size": str(len(data)),
                "Content-Type": "application/octet-stream",
            },
            content=data,
        )
        body = _check(r, "rupload")
        if not body.get("success"):
            raise FacebookError(f"rupload did not confirm the upload: {body}")

    def publish_reel(self, path: Path, description: str, title: str = "", ai_generated: bool = False) -> str:
        video_id = self._graph("POST", f"{self.page_id}/video_reels", upload_phase="start")["video_id"]
        self._upload(video_id, path)
        params: dict[str, Any] = {
            "video_id": video_id,
            "upload_phase": "finish",
            "video_state": "PUBLISHED",
            "description": description[:DESCRIPTION_MAX],
            "is_ai_generated": "true" if ai_generated else "false",
        }
        if title:
            params["title"] = title
        if not self._graph("POST", f"{self.page_id}/video_reels", **params).get("success"):
            raise FacebookError(f"finish phase for {video_id} was not accepted")
        self.wait_published(video_id)
        log.info("facebook: published reel %s", video_id)
        return video_id

    def wait_published(self, video_id: str) -> None:
        """Facebook processes the reel after 'finish'. Raise on an error; a reel still processing at
        the timeout is left to finish on its own (it publishes when ready)."""
        deadline = time.monotonic() + self.poll_timeout
        while True:
            status = self._graph("GET", video_id, fields="status").get("status") or {}
            for phase in ("uploading_phase", "processing_phase", "publishing_phase"):
                p = status.get(phase) or {}
                if p.get("status") == "error" or p.get("error"):
                    raise FacebookError(f"reel {video_id} failed in {phase}: {p.get('error') or p}")
            if status.get("video_status") in {"error", "expired", "upload_failed"}:
                raise FacebookError(f"reel {video_id}: {status}")
            pub = status.get("publishing_phase") or {}
            if pub.get("publish_status") == "published" or pub.get("status") in {"complete", "completed"}:
                return
            if time.monotonic() > deadline:
                log.warning(
                    "facebook reel %s still processing after %.0fs; it will publish when ready",
                    video_id,
                    self.poll_timeout,
                )
                return
            time.sleep(self.poll_interval)


class DryRunFacebook:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    def publish_reel(self, path: Path, description: str, title: str = "", ai_generated: bool = False) -> str:
        self.posts.append(
            {"path": path, "description": description, "title": title, "ai_generated": ai_generated}
        )
        return f"fb-dryrun-{len(self.posts)}"

    def page_name(self) -> str:
        return "dry run"


def reel_url(video_id: str) -> str:
    return f"https://www.facebook.com/reel/{video_id}"
