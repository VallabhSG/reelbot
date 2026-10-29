import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from reelbot.factory import build
from reelbot.models import JobStatus
from reelbot.youtube import TOKEN_URL, UPLOAD_URL, YouTubeError, YouTubePublisher, fit_tags


def yt():
    return YouTubePublisher("cid", "secret", "refresh")


@respx.mock
def test_resumable_upload_sends_metadata_then_bytes(tmp_path):
    video = tmp_path / "final.mp4"
    video.write_bytes(b"x" * 1234)
    token = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "AT", "expires_in": 3600})
    )
    start = respx.post(UPLOAD_URL).mock(
        return_value=httpx.Response(200, headers={"Location": "https://upload.example/session1"})
    )
    put = respx.put("https://upload.example/session1").mock(
        return_value=httpx.Response(200, json={"id": "VID1"})
    )

    vid = yt().upload_short(video, "T" * 150, "desc", ["#anime", "#aianimation"], synthetic_media=True)

    assert vid == "VID1" and token.call_count == 1  # the access token is reused for both requests
    meta = json.loads(start.calls[0].request.content)
    assert start.calls[0].request.url.params["uploadType"] == "resumable"
    assert len(meta["snippet"]["title"]) == 100 and meta["snippet"]["tags"] == ["anime", "aianimation"]
    assert meta["status"] == {
        "privacyStatus": "public",
        "selfDeclaredMadeForKids": False,
        "containsSyntheticMedia": True,
    }
    assert start.calls[0].request.headers["X-Upload-Content-Length"] == "1234"
    assert put.calls[0].request.content == b"x" * 1234
    assert put.calls[0].request.headers["Authorization"] == "Bearer AT"


@respx.mock
def test_expired_refresh_token_says_how_to_fix_it():
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(YouTubeError, match="In production"):
        yt().access_token()


@respx.mock
def test_quota_errors_are_not_retried(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "AT"}))
    start = respx.post(UPLOAD_URL).mock(
        return_value=httpx.Response(403, json={"error": {"errors": [{"reason": "quotaExceeded"}]}})
    )
    with pytest.raises(YouTubeError, match="quotaExceeded"):
        yt().upload_short(video, "t", "d", [])
    assert start.call_count == 1


def test_tags_fit_youtubes_limit():
    tags = fit_tags([f"#tag{i:03d}xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" for i in range(30)])
    assert sum(len(t) + 1 for t in tags) <= 450 and tags[0].startswith("tag000")


# ------------------------------------------------------------------ pipeline
def _approved(app):
    plan = app.pipeline.planner.plan(1, [])
    jid = app.db.create_job(1, plan)
    video = app.settings.data_dir / "v.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"mp4")
    app.db.update_job(
        jid,
        status=JobStatus.APPROVED,
        video_path=str(video),
        caption="c",
        publish_after="2000-01-01T00:00:00+00:00",
    )
    return jid


def test_publishing_also_posts_the_short(dry_settings):
    app = build(dry_settings)
    jid = _approved(app)
    app.pipeline.publish_due()
    up = app.pipeline.youtube.uploads[0]
    assert app.db.get_job(jid)["status"] == JobStatus.PUBLISHED
    assert app.db.kv_get(f"youtube_id:{jid}") == "yt-dryrun-1"
    assert up["title"] and "#shorts" in up["description"] and "Episode 1" in up["description"]
    assert up["synthetic_media"] is False
    assert any("youtube.com/shorts/yt-dryrun-1" in m for m in app.pipeline.notifier.sent)
    app.pipeline.publish_due()
    assert len(app.pipeline.youtube.uploads) == 1  # never twice


class FlakyYouTube:
    def __init__(self, fails):
        self.fails, self.calls = fails, 0

    def upload_short(self, *a, **k):
        self.calls += 1
        if self.calls <= self.fails:
            raise RuntimeError("quotaExceeded")
        return "VID"


def test_a_youtube_failure_never_blocks_instagram_and_retries_three_times(dry_settings):
    app = build(dry_settings)
    app.pipeline.youtube = FlakyYouTube(fails=5)
    jid = _approved(app)
    app.pipeline.publish_due()
    assert app.db.get_job(jid)["status"] == JobStatus.PUBLISHED  # Instagram went out
    assert "Trying again" in app.pipeline.notifier.sent[-1]
    for _ in range(5):
        app.pipeline.publish_due()
    assert app.pipeline.youtube.calls == 3 and not app.db.kv_get(f"youtube_id:{jid}")


def test_retry_succeeds_on_a_later_run(dry_settings):
    app = build(dry_settings)
    app.pipeline.youtube = FlakyYouTube(fails=1)
    jid = _approved(app)
    app.pipeline.publish_due()
    app.pipeline.publish_due(datetime.now(UTC) + timedelta(minutes=30))
    assert app.db.kv_get(f"youtube_id:{jid}") == "VID"


def test_youtube_off_without_credentials(tmp_path):
    from reelbot.config import Settings

    s = Settings(_env_file=None, dry_run=False, data_dir=tmp_path)
    assert not s.youtube_enabled
    s = Settings(_env_file=None, youtube_client_id="a", youtube_client_secret="b", youtube_refresh_token="c")
    assert s.youtube_enabled


def test_youtube_command_backfills_an_already_posted_reel(dry_settings, monkeypatch, capsys):
    from reelbot.cli import main

    monkeypatch.setattr("reelbot.cli.get_settings", lambda: dry_settings)
    app = build(dry_settings)
    jid = _approved(app)
    app.db.update_job(jid, status=JobStatus.PUBLISHED)
    assert main(["youtube", str(jid)]) == 0
    assert "youtube.com/shorts/yt-dryrun-1" in capsys.readouterr().out
