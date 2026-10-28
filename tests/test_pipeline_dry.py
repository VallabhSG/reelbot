"""End-to-end dry run (v2): real ffmpeg + Pillow, stub generators, no network."""

import json
from pathlib import Path

import pytest
from PIL import Image

from reelbot.assemble import probe_duration
from reelbot.factory import build
from reelbot.models import JobStatus
from reelbot.pipeline import CastNotReady

from .conftest import needs_ffmpeg


def _cast(app):
    out = app.pipeline.propose_cast()
    assert set(out) == {"Kai"} and len(out["Kai"]) == app.series.cast.candidates
    assert sum("🎭 Kai · option" in m for m in app.pipeline.notifier.sent) == app.series.cast.candidates
    app.pipeline.approve_cast("Kai", 1)
    assert app.pipeline.cast_missing() == []


@needs_ffmpeg
def test_full_cycle_v2(dry_settings, series):
    app = build(dry_settings, series)
    p = app.pipeline
    _cast(app)

    job = p.plan_next()
    out = p.generate(job)
    assert probe_duration(out) == pytest.approx(series.shots.count * series.shots.duration_s, abs=0.1)
    assert app.db.get_job(job)["status"] == JobStatus.AWAITING_REVIEW

    # every shot got the approved Kai sheet (when Kai is in it) + the episode's single location plate
    video_calls = [c for kind, c in p.media.calls if kind == "video"]
    plate = app.db.kv_get(f"plate:{job}")
    kai = p.cast_urls()["Kai"]
    assert len(video_calls) == series.shots.count
    assert all(c["image_urls"][-1] == plate for c in video_calls)
    assert any(kai in c["image_urls"] for c in video_calls)
    assert all("@image_" in c["prompt"] for c in video_calls)

    cover = Path(app.db.kv_get(f"cover:{job}"))
    with Image.open(cover) as im:
        assert im.format == "JPEG" and im.size == (1080, 1920)
    assert any(m.startswith("PHOTO") and "Cover" in m for m in p.notifier.sent)
    assert p.notifier.sent[-1].startswith("VIDEO")

    p.regenerate_shot(job, 1)
    assert app.db.shots(job)[1]["attempts"] == 2
    assert (
        len([1 for kind, _ in p.media.calls if kind == "image" and "Empty environment" in _["prompt"]]) == 1
    )

    caption = p.set_hook(job, "New hook.\nSecond line.")
    p.approve(job)
    assert p.publish_due() == ["dryrun-1"]
    assert p.publisher.published[0][1] == caption
    assert p.publisher.covers[0] and p.publisher.covers[0].endswith(".jpg")


@needs_ffmpeg
def test_generation_refuses_without_approved_cast(dry_settings, series):
    p = build(dry_settings, series).pipeline
    job = p.plan_next()
    with pytest.raises(CastNotReady):
        p.generate(job)
    assert p.db.get_job(job)["status"] == JobStatus.FAILED
    assert "run cast first" in p.notifier.sent[-1]
    assert not [1 for kind, _ in p.media.calls if kind == "video"]  # nothing paid for


def test_approve_cast_validates(dry_settings, series):
    p = build(dry_settings, series).pipeline
    with pytest.raises(ValueError, match="run cast first"):
        p.approve_cast("Kai", 0)


def test_lock_cast_clears_every_pick_button_and_confirms(dry_settings, series):
    p = build(dry_settings, series).pipeline
    cleared = []
    p.notifier.clear_keyboard = cleared.append
    p.propose_cast()
    msgs = json.loads(p.db.kv_get("cast_msgs:Kai"))
    assert len(msgs) == series.cast.candidates + 1  # each option photo + the "pick one" message
    text = p.lock_cast("Kai", 2)
    assert cleared == msgs
    assert text == "🎭 Kai locked in (option 3). Cast complete, episodes are unblocked."
    assert (
        p.notifier.sent[-1] == text
        and p.cast_urls()["Kai"] == json.loads(p.db.kv_get("cast_candidates:Kai"))[2]
    )


@needs_ffmpeg
def test_rejected_episode_number_is_reused(dry_settings, series):
    app = build(dry_settings, series)
    _cast(app)
    p = app.pipeline
    a = p.plan_next()
    p.generate(a)
    p.reject(a)
    assert p.db.get_job(p.plan_next())["episode_no"] == 1


def test_caption_shape_and_limits(dry_settings, series):
    p = build(dry_settings, series).pipeline
    series.caption.hashtags = [f"#t{i}" for i in range(40)]
    from reelbot.models import EpisodePlan

    plan = EpisodePlan.model_validate(
        p.planner.llm.json_chat(p.planner.system_prompt(), "SHOT_COUNT_JSON=5\n")
    )
    cap = p.caption(plan, 7)
    assert cap.splitlines()[:3] == ["NIGHT SHIFT", "Night 7 / 100", "🚇 Metro night driver"]
    assert cap.count("#t") == 30 and "\n\n\n" not in cap
