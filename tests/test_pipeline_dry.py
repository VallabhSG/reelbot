"""End-to-end dry run: real ffmpeg, stub generators, no network."""

from pathlib import Path

import pytest

from reelbot.assemble import probe_duration
from reelbot.factory import build
from reelbot.models import JobStatus

from .conftest import needs_ffmpeg


@needs_ffmpeg
def test_full_cycle(dry_settings, series):
    app = build(dry_settings, series)
    p = app.pipeline

    job = p.plan_next()
    out = p.generate(job)
    assert out.exists()
    assert probe_duration(out) == pytest.approx(series.shots.count * series.shots.duration_s, abs=0.1)
    assert app.db.get_job(job)["status"] == JobStatus.AWAITING_REVIEW
    assert "VIDEO" in p.notifier.sent[-1]

    first_kf = Path(app.db.shots(job)[1]["local_path"]).stat().st_mtime_ns
    p.regenerate_shot(job, 1)
    assert app.db.shots(job)[1]["attempts"] == 2
    assert Path(app.db.shots(job)[1]["local_path"]).stat().st_mtime_ns >= first_kf

    caption = p.set_hook(job, "New hook.\nSecond line.")
    assert "New hook." in caption and caption.startswith("NIGHT SHIFT")

    p.approve(job)
    assert p.publish_due() == ["dryrun-1"]
    j = app.db.get_job(job)
    assert j["status"] == JobStatus.PUBLISHED and j["ig_media_id"] == "dryrun-1"
    assert p.publisher.published[0][1] == caption

    with pytest.raises(ValueError):
        p.approve(job)  # can't approve twice


@needs_ffmpeg
def test_rejected_episode_number_is_reused(dry_settings, series):
    p = build(dry_settings, series).pipeline
    a = p.plan_next()
    p.generate(a)
    p.reject(a)
    b = p.plan_next()
    assert p.db.get_job(b)["episode_no"] == 1


def test_caption_shape_and_limits(dry_settings, series):
    p = build(dry_settings, series).pipeline
    series.caption.hashtags = [f"#t{i}" for i in range(40)]
    plan = p.planner.llm.json_chat("", "SHOT_COUNT_JSON=3\n")
    from reelbot.models import EpisodePlan

    cap = p.caption(EpisodePlan.model_validate(plan), 7)
    assert cap.splitlines()[:3] == ["NIGHT SHIFT", "Night 7 / 100", "🚇 Metro night driver"]
    assert cap.count("#t") == 30
    assert "\n\n\n" not in cap
