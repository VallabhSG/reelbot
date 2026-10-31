from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import respx

from reelbot.facebook import GRAPH, FacebookPublisher
from reelbot.factory import build
from reelbot.learn import day_report
from reelbot.models import JobStatus
from reelbot.youtube import API_URL, TOKEN_URL, YouTubePublisher

IST = ZoneInfo("Asia/Kolkata")
BASE = datetime(2026, 11, 3, 14, 0, tzinfo=UTC)  # Tue 19:30 IST


def _app(dry_settings, rotate=False, stagger=("instagram", "youtube", "facebook")):
    app = build(dry_settings)
    app.series.schedule.stagger = list(stagger)
    app.series.schedule.rotate = rotate
    return app


def _review_job(app):
    plan = app.pipeline.planner.plan(app.db.next_episode_no(), [])
    jid = app.db.create_job(app.db.next_episode_no(), plan)
    video = app.settings.data_dir / f"v{jid}.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"mp4")
    app.db.update_job(jid, status=JobStatus.AWAITING_REVIEW, video_path=str(video), caption="cap")
    return jid


def test_one_platform_per_day_from_the_approval_slot(dry_settings):
    app = _app(dry_settings)
    slots = app.pipeline.release_slots(BASE)
    assert slots == {
        "instagram": "2026-11-03T14:00:00+00:00",
        "youtube": "2026-11-04T14:00:00+00:00",
        "facebook": "2026-11-05T14:00:00+00:00",
    }


def test_rotation_gives_every_platform_every_offset_over_three_weeks(dry_settings):
    app = _app(dry_settings, rotate=True)
    offsets = {p: set() for p in ("instagram", "youtube", "facebook")}
    for week in range(3):
        base = BASE + timedelta(weeks=week)
        for p, t in app.pipeline.release_slots(base).items():
            offsets[p].add((datetime.fromisoformat(t) - base).days)
    assert all(v == {0, 1, 2} for v in offsets.values())


def test_platforms_that_are_off_take_no_day(dry_settings):
    app = _app(dry_settings)
    app.pipeline.facebook = None
    assert set(app.pipeline.release_slots(BASE)) == {"instagram", "youtube"}


def test_no_stagger_means_everything_at_once(dry_settings):
    app = _app(dry_settings, stagger=())
    assert len(set(app.pipeline.release_slots(BASE).values())) == 1


def test_publishing_follows_the_slots_day_by_day(dry_settings):
    app = _app(dry_settings)
    jid = _review_job(app)
    app.pipeline.approve(jid, BASE)
    p = app.pipeline
    p.publish_due(BASE + timedelta(minutes=1))
    assert app.db.get_job(jid)["status"] == JobStatus.PUBLISHED
    assert not p.youtube.uploads and not p.facebook.posts
    p.publish_due(BASE + timedelta(days=1, minutes=1))
    assert len(p.youtube.uploads) == 1 and not p.facebook.posts
    p.publish_due(BASE + timedelta(days=2, minutes=1))
    assert len(p.facebook.posts) == 1
    p.publish_due(BASE + timedelta(days=3))
    assert len(p.youtube.uploads) == 1 and len(p.facebook.posts) == 1  # once each


def test_a_platform_can_go_before_instagram(dry_settings):
    app = _app(dry_settings, stagger=("youtube", "instagram", "facebook"))
    jid = _review_job(app)
    app.pipeline.approve(jid, BASE)
    app.pipeline.publish_due(BASE + timedelta(minutes=1))
    assert len(app.pipeline.youtube.uploads) == 1
    assert app.db.get_job(jid)["status"] == JobStatus.APPROVED  # Instagram's day is tomorrow
    app.pipeline.publish_due(BASE + timedelta(days=1, minutes=1))
    assert app.db.get_job(jid)["status"] == JobStatus.PUBLISHED


def test_approval_message_lists_the_plan(dry_settings):
    app = _app(dry_settings)
    jid = _review_job(app)
    app.pipeline.approve(jid, BASE)
    assert (
        app.pipeline.release_plan_text(jid, IST)
        == "Instagram Tue 19:30 · YouTube Wed 19:30 · Facebook Thu 19:30"
    )


def test_reels_approved_before_staggering_still_cross_post_right_after_instagram(dry_settings):
    app = _app(dry_settings)
    jid = _review_job(app)
    app.db.update_job(jid, status=JobStatus.APPROVED, publish_after="2000-01-01T00:00:00+00:00")  # no slots
    app.pipeline.publish_due()
    assert len(app.pipeline.youtube.uploads) == 1 and len(app.pipeline.facebook.posts) == 1


def test_cross_post_stats_and_views_by_day(dry_settings):
    app = _app(dry_settings)
    jid = _review_job(app)
    old = datetime.now(UTC) - timedelta(days=5)
    app.pipeline.approve(jid, old)
    app.pipeline.publish_due(old + timedelta(days=3))
    assert app.pipeline.learner.refresh_metrics() >= 2
    assert {p for _, p in app.db.platform_metrics()} == {"youtube", "facebook"}
    report = day_report(app.db, IST)
    assert report.startswith("📅 Views by posting day") and "YouTube:" in report and "Facebook:" in report
    assert "mostly noise" in report


@respx.mock
def test_youtube_stats():
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "AT"}))
    respx.get(f"{API_URL}/videos").mock(
        return_value=httpx.Response(
            200,
            json={"items": [{"statistics": {"viewCount": "1520", "likeCount": "88", "commentCount": "4"}}]},
        )
    )
    assert YouTubePublisher("a", "b", "c").stats("V") == {"views": 1520.0, "likes": 88.0, "comments": 4.0}


@respx.mock
def test_facebook_stats_take_the_reel_play_count():
    respx.get(f"{GRAPH}/v26.0/R1/video_insights").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"name": "blue_reels_play_count", "values": [{"value": 940}]},
                    {"name": "post_video_avg_time_watched", "values": [{"value": 6100}]},
                    {"name": "post_video_reactions_by_type_total", "values": [{"value": {"like": 3}}]},
                ]
            },
        )
    )
    assert FacebookPublisher("P", "T").stats("R1") == {"views": 940.0, "avg_watch_ms": 6100.0}


def test_turning_staggering_off_moves_an_approved_reel_to_its_first_slot(dry_settings):
    app = _app(dry_settings, rotate=True)
    jid = _review_job(app)
    app.pipeline.approve(jid, BASE)
    staggered = app.pipeline.slots(jid)
    assert len(set(staggered.values())) == 3
    app.series.schedule.stagger = []  # every platform at once from now on
    slots = app.pipeline.slots(jid)
    assert set(slots.values()) == {min(staggered.values())} and set(slots) == set(staggered)
    p = app.pipeline
    p.publish_due(datetime.fromisoformat(min(staggered.values())) + timedelta(minutes=1))
    assert app.db.get_job(jid)["status"] == JobStatus.PUBLISHED
    assert p.crosspost_platforms() and all(  # every platform went out in the same run
        app.db.kv_get(f"{x}_id:{jid}") for x in p.crosspost_platforms()
    )
