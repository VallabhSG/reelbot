"""End-to-end dry run (v2): real ffmpeg + Pillow, stub generators, no network."""

import json
from pathlib import Path

import pytest
from PIL import Image

from reelbot.assemble import probe_duration
from reelbot.factory import build
from reelbot.models import JobStatus
from reelbot.pipeline import CastNotReady, limit_hashtags
from reelbot.series import Affiliate, load_series

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
    assert cap.count("#t") == 5 and "\n\n\n" not in cap  # Instagram caps hashtags at 5


def test_captions_keep_only_five_hashtags():
    from reelbot.pipeline import limit_hashtags

    cap = "BOSS 1 / 100\n\nThe doors always wait.\n\nAnimated with Kling\n\n#a #b #c #d #e #f #g"
    out = limit_hashtags(cap)
    assert out.endswith("#a #b #c #d #e") and "#f" not in out
    assert "The doors always wait." in out and "\n\n" in out
    assert limit_hashtags("no tags here") == "no tags here"
    assert limit_hashtags("caf&#233; #one") == "caf&#233; #one"


def test_affiliate_line_sits_above_the_hashtags_on_every_platform(dry_settings, series):
    p = build(dry_settings, series).pipeline
    cap = "Ep 1\n\nThe doors always wait.\n\nAnimated with Kling\n\n#anime #aianimation"
    series.caption.affiliates = []
    assert p.with_affiliate(cap, "instagram") == cap  # no link set: nothing added
    series.caption.affiliates = [
        Affiliate(
            url="https://higgsfield.ai/?ref=val",
            instagram="🎬 Higgsfield · affiliate link in bio",
            link_text="🎬 Higgsfield (affiliate link): {url}",
        ),
        Affiliate(
            url="https://try.elevenlabs.io/x",
            instagram="🎵 ElevenLabs · affiliate link in bio",
            link_text="🎵 ElevenLabs (affiliate link): {url}",
        ),
    ]
    ig = p.with_affiliate(cap, "instagram")
    assert ig.endswith(
        "Kling\n\n🎬 Higgsfield · affiliate link in bio\n🎵 ElevenLabs · affiliate link in bio\n\n#anime #aianimation"
    )
    assert "https://" not in ig
    yt = p.with_affiliate(cap, "youtube")
    assert (
        "(affiliate link): https://higgsfield.ai/?ref=val\n🎵 ElevenLabs (affiliate link): https://try.elevenlabs.io/x\n\n#anime"
        in yt
    )
    assert p.with_affiliate(yt, "youtube") == yt  # never twice
    assert p.with_affiliate("no tags", "facebook").endswith("https://try.elevenlabs.io/x")
    assert limit_hashtags(ig).count("#") == 2  # the links never eat the hashtag allowance


def test_series_file_carries_both_affiliate_links_on_every_platform():
    cap = load_series(Path(__file__).parents[1] / "series" / "everyday-boss-fights.yaml").caption
    lines = cap.affiliate_lines("youtube")
    assert any("higgsfield.ai" in x for x in lines) and any("elevenlabs.io" in x for x in lines)
    assert all("(affiliate link): https://" in x for x in lines)
    assert cap.affiliate_lines("instagram") == lines  # the links themselves, not "in bio"


def test_a_dated_special_takes_its_day_without_using_up_a_numbered_entry(dry_settings, series, monkeypatch):
    from datetime import date

    from reelbot import pipeline as pl
    from reelbot.series import EpisodeSeed, SpecialSeed

    series.episodes = [EpisodeSeed(category=c) for c in ("A", "B", "C")]
    day = date(2026, 11, 5)
    series.specials = [SpecialSeed(category="Diwali ki Safai", emoji="🪔", date=day, card_label="DUST")]
    app = build(dry_settings, series)
    p = app.pipeline
    plan = p.planner.plan(1, [])
    j1 = app.db.create_job(1, plan)
    assert p._seed(j1).category == "A"
    seed, special = p.seed_for_episode(2, day)
    assert seed.category == "Diwali ki Safai" and special == "2026-11-05"
    j2 = app.db.create_job(2, plan)
    app.db.kv_set("specials", json.dumps({str(j2): special}))
    assert p.seed_for_episode(3, day)[1] == ""  # once per date
    assert p.seed_for_episode(3, date(2026, 11, 6))[0].category == "B"  # B waited, nothing skipped
    j3 = app.db.create_job(3, plan)
    assert (p._seed(j2).category, p._seed(j3).category) == ("Diwali ki Safai", "B")
    labels = []
    monkeypatch.setattr(pl, "render_title_card", lambda t, sub, *a: labels.append((t, sub)) or Path("x"))
    p._boss_card(j2, plan)
    assert labels[-1][1] == "DUST"
    app.db.update_job(j2, status=JobStatus.REJECTED)  # a rejected special frees its day again
    assert p.seed_for_episode(2, day)[1] == "2026-11-05"


def test_series_file_pins_diwali_specials_to_episode_days():
    s = load_series(Path(__file__).parents[1] / "series" / "everyday-boss-fights.yaml")
    days = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
    names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    assert s.specials and all(names[sp.date.weekday()] in s.schedule.days for sp in s.specials)
    assert days >= set(s.schedule.days)
