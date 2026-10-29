"""Live connectivity check for every service. Spends one Soul image (~$0.003). Posts nothing publicly."""

from __future__ import annotations

import shutil
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx

from .budget import BudgetGuard
from .config import Settings
from .db import DB
from .series import load_series


def _row(ok: bool, name: str, detail: str) -> bool:
    print(f"{'✓' if ok else '✗'} {name:<14} {detail}")
    return ok


def _try(name: str, fn: Callable[[], str]) -> bool:
    try:
        return _row(True, name, fn())
    except Exception as e:  # report and keep going: we want the full picture in one run
        return _row(False, name, f"{type(e).__name__}: {str(e)[:300]}")


def run_smoke(s: Settings) -> int:
    series = load_series(s.series_file)
    db = DB(s.db_path)
    guard = BudgetGuard(db, series.budget, s.timezone)
    results: list[bool] = []

    def ffmpeg() -> str:
        path = shutil.which("ffmpeg")
        if not path:
            raise RuntimeError("ffmpeg not on PATH")
        return path

    results.append(_try("ffmpeg", ffmpeg))

    def llm() -> str:
        from .providers.llm import LLMClient

        out = LLMClient(s.llm_base_url, s.llm_api_key, s.llm_model).json_chat(
            "Reply with JSON only.", 'Return {"ok": true}', temperature=0
        )
        assert out.get("ok") is True, f"unexpected reply {out}"
        return f"{s.llm_model} answered"

    results.append(_try("LLM", llm))

    def higgsfield() -> str:
        from .providers.higgsfield import HiggsfieldClient, HiggsfieldMedia

        media = HiggsfieldMedia(
            HiggsfieldClient(s.hf_api_key_id, s.hf_api_key_secret, s.hf_base_url),
            series.models,
            guard,
            series.base_dir,
        )
        url = media.image(
            {
                "prompt": f"test frame: quiet neon street at night. {series.style.bible}",
                "aspect_ratio": "9:16",
                "image_urls": [],
                "negative_prompt": None,
            },
            job_id=None,
        )
        return f"{series.models.image.endpoint} -> {url[:60]}…"

    results.append(_try("Higgsfield", higgsfield))

    def openrouter() -> str:
        if not series.uses_provider("openrouter"):
            return "skipped (no model in the series file uses OpenRouter)"
        from .providers.openrouter_video import OpenRouterVideo

        client = OpenRouterVideo(s.openrouter_api_key, s.openrouter_base_url)
        key = client.key_info()
        names = [n for n in series.take_model_names() if series.models.takes[n].provider == "openrouter"]
        for n in names:
            client.video_model(series.models.takes[n].endpoint)
        limit = key.get("limit_remaining", key.get("limit"))
        left = f", ${float(limit):.2f} left on the key" if isinstance(limit, int | float) else ""
        models = ", ".join(series.models.takes[n].endpoint for n in names)
        return f"key ok ({models} available{left}); no video rendered"

    results.append(_try("OpenRouter", openrouter))

    def elevenlabs() -> str:
        if series.music.provider != "elevenlabs":
            return f"skipped (music.provider={series.music.provider})"
        r = httpx.get(
            "https://api.elevenlabs.io/v1/user/subscription",
            headers={"xi-api-key": s.elevenlabs_api_key},
            timeout=20,
        )
        r.raise_for_status()
        tier = r.json().get("tier", "?")
        if tier == "free":
            raise RuntimeError("key works but plan is 'free'; the Music API needs a paid plan")
        return f"plan: {tier}"

    results.append(_try("ElevenLabs", elevenlabs))

    def media_bucket() -> str:
        from .storage import S3Storage

        st = S3Storage(s)
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "smoke.mp4"
            f.write_bytes(b"reelbot-smoke-" + uuid.uuid4().hex.encode())
            key, url = st.put_public(f, "smoke")
            try:
                r = httpx.get(url, timeout=20, follow_redirects=True)
                if r.status_code != 200 or r.content != f.read_bytes():
                    raise RuntimeError(
                        f"uploaded but public URL returned {r.status_code}; enable public access"
                    )
            finally:
                st.delete(key)
        return f"{s.s3_bucket}: upload + public read OK"

    results.append(_try("R2 media", media_bucket))

    def state_bucket() -> str:
        if not s.s3_state_bucket:
            return "skipped (S3_STATE_BUCKET not set; only needed on cron hosts)"
        from .state import StateSync

        sync = StateSync(s)
        key = f"state/smoke-{uuid.uuid4().hex}"
        sync.s3.put_object(Bucket=sync.bucket, Key=key, Body=b"ok")
        sync.s3.delete_object(Bucket=sync.bucket, Key=key)
        return f"{s.s3_state_bucket}: read/write OK"

    results.append(_try("R2 state", state_bucket))

    def telegram() -> str:
        from .telegram import Telegram

        tg = Telegram(s.telegram_bot_token, s.telegram_chat_id)
        me = tg._call("getMe")
        tg.send_message("✅ reelbot connected. Previews will arrive here.")
        return f"@{me.get('username')} -> chat {s.telegram_chat_id} (check Telegram)"

    results.append(_try("Telegram", telegram))

    def instagram() -> str:
        from .instagram import InstagramPublisher

        pub = InstagramPublisher(
            s.ig_user_id, s.ig_access_token, db, base_url=s.ig_graph_base_url, version=s.ig_graph_version
        )
        me = pub._req("GET", "me", fields="user_id,username,account_type")
        if str(me.get("user_id")) != str(s.ig_user_id):
            raise RuntimeError(f"token belongs to user_id {me.get('user_id')}, IG_USER_ID is {s.ig_user_id}")
        return f"@{me.get('username')} ({me.get('account_type', 'professional')})"

    results.append(_try("Instagram", instagram))

    if s.youtube_enabled:

        def youtube() -> str:
            from .youtube import YouTubePublisher

            yt = YouTubePublisher(s.youtube_client_id, s.youtube_client_secret, s.youtube_refresh_token)
            return f"channel '{yt.channel_title()}' (uploads go here)"

        results.append(_try("YouTube", youtube))

    if s.facebook_enabled:

        def facebook() -> str:
            from .facebook import FacebookPublisher

            fb = FacebookPublisher(s.fb_page_id, s.fb_page_token, s.fb_graph_version)
            return f"Page '{fb.page_name()}' (reels go here)"

        results.append(_try("Facebook", facebook))

    print(f"\n{sum(results)}/{len(results)} checks passed · month spend ${guard.month_spend():.4f}")
    return 0 if all(results) else 1
