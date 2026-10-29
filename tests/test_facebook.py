from urllib.parse import parse_qs

import httpx
import pytest
import respx

from reelbot.facebook import GRAPH, RUPLOAD, FacebookError, FacebookPublisher
from reelbot.factory import build
from reelbot.models import JobStatus

V = "v26.0"


def fb(**kw):
    return FacebookPublisher("PAGE", "PTOKEN", V, poll_interval=0, poll_timeout=5, **kw)


@respx.mock
def test_reel_start_upload_finish_then_wait_until_published(tmp_path):
    video = tmp_path / "final.mp4"
    video.write_bytes(b"v" * 321)
    reels = respx.post(f"{GRAPH}/{V}/PAGE/video_reels").mock(
        side_effect=[
            httpx.Response(200, json={"video_id": "VID"}),
            httpx.Response(200, json={"success": True}),
        ]
    )
    upload = respx.post(f"{RUPLOAD}/{V}/VID").mock(return_value=httpx.Response(200, json={"success": True}))
    respx.get(f"{GRAPH}/{V}/VID").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "status": {"video_status": "processing", "publishing_phase": {"status": "in_progress"}}
                },
            ),
            httpx.Response(
                200,
                json={
                    "status": {
                        "video_status": "ready",
                        "publishing_phase": {"status": "complete", "publish_status": "published"},
                    }
                },
            ),
        ]
    )

    assert fb().publish_reel(video, "caption #anime", title="Doors wait 🚇") == "VID"

    start = parse_qs(reels.calls[0].request.content.decode())
    finish = parse_qs(reels.calls[1].request.content.decode())
    assert start["upload_phase"] == ["start"] and start["access_token"] == ["PTOKEN"]
    assert finish["upload_phase"] == ["finish"] and finish["video_state"] == ["PUBLISHED"]
    assert finish["description"] == ["caption #anime"] and finish["is_ai_generated"] == ["false"]
    h = upload.calls[0].request.headers
    assert h["Authorization"] == "OAuth PTOKEN" and h["file_size"] == "321" and h["offset"] == "0"
    assert upload.calls[0].request.content == b"v" * 321


@respx.mock
def test_processing_error_is_raised_with_facebooks_reason(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"v")
    respx.post(f"{GRAPH}/{V}/PAGE/video_reels").mock(
        side_effect=[
            httpx.Response(200, json={"video_id": "VID"}),
            httpx.Response(200, json={"success": True}),
        ]
    )
    respx.post(f"{RUPLOAD}/{V}/VID").mock(return_value=httpx.Response(200, json={"success": True}))
    respx.get(f"{GRAPH}/{V}/VID").mock(
        return_value=httpx.Response(
            200,
            json={"status": {"processing_phase": {"status": "error", "error": {"message": "aspect ratio"}}}},
        )
    )
    with pytest.raises(FacebookError, match="aspect ratio"):
        fb().publish_reel(video, "c")


@respx.mock
def test_bad_token_is_not_retried():
    route = respx.get(f"{GRAPH}/{V}/PAGE").mock(
        return_value=httpx.Response(
            400, json={"error": {"message": "Invalid OAuth access token", "code": 190}}
        )
    )
    with pytest.raises(FacebookError, match="Invalid OAuth"):
        fb().page_name()
    assert route.call_count == 1


def test_published_reel_goes_to_every_platform_once(dry_settings):
    app = build(dry_settings)
    plan = app.pipeline.planner.plan(1, [])
    jid = app.db.create_job(1, plan)
    video = app.settings.data_dir / "v.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"mp4")
    app.db.update_job(
        jid,
        status=JobStatus.APPROVED,
        video_path=str(video),
        caption="the IG caption",
        publish_after="2000-01-01T00:00:00+00:00",
    )
    app.pipeline.publish_due()
    app.pipeline.publish_due()
    post = app.pipeline.facebook.posts
    assert len(post) == 1 and post[0]["description"] == "the IG caption" and post[0]["ai_generated"] is False
    assert app.db.kv_get(f"facebook_id:{jid}") == "fb-dryrun-1" and app.db.kv_get(f"youtube_id:{jid}")
    assert any("facebook.com/reel/fb-dryrun-1" in m for m in app.pipeline.notifier.sent)


def test_one_platform_failing_does_not_stop_the_other(dry_settings):
    class Down:
        def upload_short(self, *a, **k):
            raise RuntimeError("channel suspended")

    app = build(dry_settings)
    app.pipeline.youtube = Down()
    plan = app.pipeline.planner.plan(1, [])
    jid = app.db.create_job(1, plan)
    video = app.settings.data_dir / "v.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"mp4")
    app.db.update_job(jid, status=JobStatus.PUBLISHED, video_path=str(video), caption="c")
    out = app.pipeline.cross_post(jid)
    assert out == {"facebook": "fb-dryrun-1"}
    assert any("YouTube upload of #" in m and "channel suspended" in m for m in app.pipeline.notifier.sent)
