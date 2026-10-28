"""Instagram Reels publishing via the Instagram API with Instagram Login (no Facebook Page needed).

Flow (developers.facebook.com/docs/instagram-platform/content-publishing):
  1. POST /{ig-user-id}/media        media_type=REELS, video_url=<public mp4>, caption -> container id
  2. GET  /{container-id}?fields=status_code   until FINISHED (or ERROR / EXPIRED)
  3. POST /{ig-user-id}/media_publish creation_id=<container id>                   -> media id
Requires a professional (Business/Creator) account and instagram_business_content_publish.
Accounts are limited to 100 API-published posts per rolling 24h.

Insights (what the series learns from): GET /{media-id}/insights?metric=... needs
instagram_business_manage_insights. Lifetime values only; they can lag up to 48h.
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from .db import DB
from .providers._http import retryable

log = logging.getLogger(__name__)

TOKEN_KEY = "ig_access_token"
TOKEN_REFRESHED_KEY = "ig_token_refreshed_at"
TOKEN_ENV_KEY = "ig_env_token_sig"  # which ENV_FILE token the stored (refreshed) copy came from

# Reels metrics on the Instagram API with Instagram Login (plays/impressions are deprecated).
# ig_reels_avg_watch_time is in milliseconds; reels_skip_rate is a percentage.
REEL_METRICS = (
    "views",
    "reach",
    "likes",
    "comments",
    "shares",
    "saved",
    "ig_reels_avg_watch_time",
    "reels_skip_rate",
)


class InstagramError(RuntimeError):
    pass


class InsightsUnavailable(InstagramError):
    """The token can't read insights (missing instagram_business_manage_insights)."""


def _is_permission_error(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in ('"code":10,', '"code":10}', '"code":200', "(#10)", "(#200)", "permission"))


class InstagramPublisher:
    def __init__(
        self,
        ig_user_id: str,
        access_token: str,
        db: DB,
        base_url: str = "https://graph.instagram.com",
        version: str = "",
        http: httpx.Client | None = None,
        poll_interval: float = 15.0,
        poll_timeout: float = 900.0,
    ):
        if not ig_user_id:
            raise ValueError("IG_USER_ID is required")
        self.ig_user_id = ig_user_id
        self.db = db
        self._env_token = access_token
        self.root = base_url.rstrip("/") + (f"/{version.strip('/')}" if version else "")
        self.token_root = base_url.rstrip("/")
        self.http = http or httpx.Client(timeout=60.0)
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout

    # ---- token handling: DB copy (refreshed) wins over the env bootstrap token ----
    def _env_sig(self) -> str:
        return hashlib.sha256(self._env_token.encode()).hexdigest()[:16]

    @property
    def token(self) -> str:
        stored = self.db.kv_get(TOKEN_KEY)
        if stored and self._env_token:
            recorded = self.db.kv_get(TOKEN_ENV_KEY)
            if recorded is None:  # copy made before we tracked this: it came from the current token
                self.db.kv_set(TOKEN_ENV_KEY, self._env_sig())
            elif recorded != self._env_sig():
                # a NEW token in ENV_FILE (e.g. with more permissions) replaces the refreshed old one
                self.db.kv_set(TOKEN_KEY, "")
                self.db.kv_set(TOKEN_ENV_KEY, self._env_sig())
                stored = ""
        t = stored or self._env_token
        if not t:
            raise ValueError("IG_ACCESS_TOKEN is required")
        return t

    def refresh_token_if_due(self, every: timedelta = timedelta(days=7)) -> bool:
        """Long-lived tokens last ~60 days; refreshing weekly keeps them alive indefinitely.

        Instagram only refreshes tokens that are at least 24h old, so a brand-new token fails here.
        A failed refresh never blocks publishing: the current token is still valid, and the next
        publish simply tries again.
        """
        last = self.db.kv_get(TOKEN_REFRESHED_KEY)
        if last and datetime.now(UTC) - datetime.fromisoformat(last) < every:
            return False
        try:
            r = self.http.get(
                f"{self.token_root}/refresh_access_token",
                params={"grant_type": "ig_refresh_token", "access_token": self.token},
            )
        except httpx.HTTPError as e:
            log.warning("token refresh skipped (network): %s", e)
            return False
        if r.status_code != 200:
            log.warning("token refresh skipped: %s %s", r.status_code, r.text[:200])
            return False
        self.db.kv_set(TOKEN_KEY, r.json()["access_token"])
        if self._env_token:
            self.db.kv_set(TOKEN_ENV_KEY, self._env_sig())
        self.db.kv_set(TOKEN_REFRESHED_KEY, datetime.now(UTC).isoformat(timespec="seconds"))
        return True

    @retry(
        retry=retry_if_exception(retryable),
        stop=stop_after_attempt(4),
        wait=wait_exponential(3, max=60),
        reraise=True,
    )
    def _req(self, method: str, path: str, **params: Any) -> dict[str, Any]:
        url = f"{self.root}/{path.lstrip('/')}"
        auth = {"access_token": self.token}
        if method == "POST":  # form body keeps long captions out of the URL
            r = self.http.request(method, url, params=auth, data=params)
        else:
            r = self.http.request(method, url, params={**params, **auth})
        if r.status_code >= 400 and not retryable(httpx.HTTPStatusError("", request=r.request, response=r)):
            raise InstagramError(f"{method} {path} -> {r.status_code}: {r.text[:500]}")
        r.raise_for_status()
        return r.json()

    def create_reel_container(
        self, video_url: str, caption: str, thumb_offset_ms: int | None, cover_url: str | None = None
    ) -> str:
        params: dict[str, Any] = {
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "share_to_feed": "true",
        }
        if cover_url:
            params["cover_url"] = cover_url
        elif thumb_offset_ms is not None:
            params["thumb_offset"] = str(thumb_offset_ms)
        return self._req("POST", f"{self.ig_user_id}/media", **params)["id"]

    def wait_container(self, container_id: str) -> None:
        deadline = time.monotonic() + self.poll_timeout
        while True:
            status = self._req("GET", container_id, fields="status_code,status").get("status_code")
            if status == "FINISHED":
                return
            if status in {"ERROR", "EXPIRED"}:
                detail = self._req("GET", container_id, fields="status")
                raise InstagramError(f"container {container_id} {status}: {detail}")
            if time.monotonic() > deadline:
                raise InstagramError(
                    f"container {container_id} still {status} after {self.poll_timeout:.0f}s"
                )
            time.sleep(self.poll_interval)

    def publish(self, container_id: str) -> str:
        return self._req("POST", f"{self.ig_user_id}/media_publish", creation_id=container_id)["id"]

    def publish_reel(
        self, video_url: str, caption: str, thumb_offset_ms: int | None = None, cover_url: str | None = None
    ) -> str:
        try:
            cid = self.create_reel_container(video_url, caption, thumb_offset_ms, cover_url)
        except InstagramError as e:
            if not cover_url:
                raise
            # a cover problem must never block the post: fall back to a frame from the video
            log.warning("container with cover_url refused (%s); retrying without it", e)
            cid = self.create_reel_container(video_url, caption, thumb_offset_ms)
        log.info("ig container %s created", cid)
        self.wait_container(cid)
        media_id = self.publish(cid)
        log.info("ig published media %s", media_id)
        return media_id

    def permalink(self, media_id: str) -> str | None:
        try:
            return self._req("GET", media_id, fields="permalink").get("permalink")
        except Exception:  # cosmetic only
            return None

    def insights(self, media_id: str) -> dict[str, float]:
        """Lifetime metrics of one reel. Metrics Instagram doesn't have yet are simply missing.
        Raises InsightsUnavailable when the token lacks the insights permission."""
        try:
            return _parse_insights(self._req("GET", f"{media_id}/insights", metric=",".join(REEL_METRICS)))
        except InstagramError as e:
            if _is_permission_error(str(e)):
                raise InsightsUnavailable(str(e)) from e
            log.info("insights batch for %s refused (%s); asking metric by metric", media_id, e)
        out: dict[str, float] = {}
        for m in REEL_METRICS:  # one unsupported metric fails the whole batch, so isolate them
            try:
                out.update(_parse_insights(self._req("GET", f"{media_id}/insights", metric=m)))
            except InstagramError as e:
                if _is_permission_error(str(e)):
                    raise InsightsUnavailable(str(e)) from e
                log.info("insight %s unavailable for %s: %s", m, media_id, str(e)[:120])
        return out


def _parse_insights(body: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for item in body.get("data") or []:
        name = item.get("name")
        values = item.get("values") or []
        value = values[0].get("value") if values else (item.get("total_value") or {}).get("value")
        if name and isinstance(value, int | float):
            out[name] = float(value)
    return out


class DryRunPublisher:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []
        self.covers: list[str | None] = []

    def refresh_token_if_due(self, every: timedelta = timedelta(days=7)) -> bool:
        return False

    def publish_reel(
        self, video_url: str, caption: str, thumb_offset_ms: int | None = None, cover_url: str | None = None
    ) -> str:
        self.published.append((video_url, caption))
        self.covers.append(cover_url)
        return f"dryrun-{len(self.published)}"

    def permalink(self, media_id: str) -> str | None:
        return None

    def insights(self, media_id: str) -> dict[str, float]:
        """Deterministic fake numbers so a dry run exercises the learning loop."""
        h = hashlib.sha256(media_id.encode()).digest()
        views = 500 + h[0] * 20
        return {
            "views": float(views),
            "reach": float(views * 0.8),
            "likes": float(h[1] % 120),
            "comments": float(h[2] % 15),
            "shares": float(h[3] % 30),
            "saved": float(h[4] % 25),
            "ig_reels_avg_watch_time": float(4000 + h[5] * 40),
            "reels_skip_rate": float(30 + h[6] % 40),
        }
