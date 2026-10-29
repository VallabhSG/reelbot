"""YouTube Shorts publishing (YouTube Data API v3).

The same 9:16 reel becomes a Short: YouTube files any vertical or square video up to 3 minutes
under Shorts. Uploads are free (videos.insert: 100 per day per Google Cloud project).

Auth: an OAuth refresh token for the channel (scopes youtube.upload + youtube.readonly), made once
in Google's OAuth Playground with your own client id/secret; see the README. The OAuth consent
screen must be "In production" or Google expires the refresh token after 7 days.
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

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
API_URL = "https://www.googleapis.com/youtube/v3"
TITLE_MAX, DESCRIPTION_MAX, TAGS_MAX_CHARS = 100, 5000, 450


class YouTubeError(RuntimeError):
    pass


def _raise(r: httpx.Response, what: str) -> None:
    if r.status_code >= 400 and not retryable(httpx.HTTPStatusError("", request=r.request, response=r)):
        raise YouTubeError(f"{what} -> {r.status_code}: {r.text[:500]}")
    r.raise_for_status()


def fit_tags(tags: list[str]) -> list[str]:
    """YouTube allows ~500 characters of tags in total; keep whole tags that fit."""
    out, used = [], 0
    for t in (t.lstrip("#").strip() for t in tags):
        if t and used + len(t) + 1 <= TAGS_MAX_CHARS:
            out.append(t)
            used += len(t) + 1
    return out


class YouTubePublisher:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        http: httpx.Client | None = None,
    ):
        if not (client_id and client_secret and refresh_token):
            raise ValueError(
                "YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET and YOUTUBE_REFRESH_TOKEN are required"
            )
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.http = http or httpx.Client(timeout=httpx.Timeout(300.0, connect=15.0))
        self._token: tuple[str, float] | None = None

    @retry(
        retry=retry_if_exception(retryable),
        stop=stop_after_attempt(4),
        wait=wait_exponential(2, max=30),
        reraise=True,
    )
    def access_token(self) -> str:
        if self._token and self._token[1] > time.monotonic() + 60:
            return self._token[0]
        r = self.http.post(
            TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if r.status_code == 400 and "invalid_grant" in r.text:
            raise YouTubeError(
                "YouTube refused the refresh token (invalid_grant): it expired or was revoked. If the "
                "OAuth consent screen is still 'Testing', Google expires tokens after 7 days: set it to "
                "'In production', make a new refresh token and update YOUTUBE_REFRESH_TOKEN."
            )
        _raise(r, "token refresh")
        body = r.json()
        self._token = (body["access_token"], time.monotonic() + float(body.get("expires_in", 3600)))
        return self._token[0]

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token()}"}

    def channel_title(self) -> str:
        r = self.http.get(
            f"{API_URL}/channels", params={"part": "snippet", "mine": "true"}, headers=self._auth()
        )
        _raise(r, "channels.list")
        items = r.json().get("items") or []
        return items[0]["snippet"]["title"] if items else "(no channel on this account)"

    def stats(self, video_id: str) -> dict[str, float]:
        """Lifetime views / likes / comments of one Short (youtube.readonly)."""
        r = self.http.get(
            f"{API_URL}/videos", params={"part": "statistics", "id": video_id}, headers=self._auth()
        )
        _raise(r, "videos.list")
        items = r.json().get("items") or []
        st = items[0].get("statistics", {}) if items else {}
        out = {
            k: _num(st.get(src))
            for k, src in (("views", "viewCount"), ("likes", "likeCount"), ("comments", "commentCount"))
        }
        return {k: v for k, v in out.items() if v is not None}

    @retry(
        retry=retry_if_exception(retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(5, max=60),
        reraise=True,
    )
    def upload_short(
        self,
        path: Path,
        title: str,
        description: str,
        tags: list[str],
        category_id: str = "1",
        made_for_kids: bool = False,
        synthetic_media: bool = False,
    ) -> str:
        """Resumable upload in two requests (session, then the bytes). Returns the video id."""
        size = path.stat().st_size
        meta: dict[str, Any] = {
            "snippet": {
                "title": title[:TITLE_MAX],
                "description": description[:DESCRIPTION_MAX],
                "tags": fit_tags(tags),
                "categoryId": category_id,
            },
            "status": {
                "privacyStatus": "public",
                "selfDeclaredMadeForKids": made_for_kids,
                "containsSyntheticMedia": synthetic_media,
            },
        }
        r = self.http.post(
            UPLOAD_URL,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={
                **self._auth(),
                "X-Upload-Content-Type": "video/mp4",
                "X-Upload-Content-Length": str(size),
            },
            json=meta,
        )
        _raise(r, "videos.insert (start)")
        session = r.headers["Location"]
        with path.open("rb") as f:
            r = self.http.put(
                session,
                content=f.read(),
                headers={**self._auth(), "Content-Type": "video/mp4", "Content-Length": str(size)},
            )
        _raise(r, "videos.insert (upload)")
        video_id = r.json()["id"]
        log.info("youtube: uploaded %s as %s", path.name, video_id)
        return video_id


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class DryRunYouTube:
    def __init__(self) -> None:
        self.uploads: list[dict[str, Any]] = []

    def upload_short(self, path: Path, title: str, description: str, tags: list[str], **kw: Any) -> str:
        self.uploads.append({"path": path, "title": title, "description": description, "tags": tags, **kw})
        return f"yt-dryrun-{len(self.uploads)}"

    def channel_title(self) -> str:
        return "dry run"

    def stats(self, video_id: str) -> dict[str, float]:
        return {"views": float(300 + sum(map(ord, video_id)) % 900), "likes": 12.0, "comments": 2.0}


def shorts_url(video_id: str) -> str:
    return f"https://youtube.com/shorts/{video_id}"
