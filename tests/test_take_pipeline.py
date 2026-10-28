"""v3 end to end in dry-run: real ffmpeg + Pillow, stub generators, no network."""

import subprocess
from pathlib import Path

import pytest
from PIL import Image

from reelbot.assemble import TakeAssembly, assemble_take, has_audio, probe_duration
from reelbot.factory import build
from reelbot.models import JobStatus
from reelbot.telegram import take_keyboard

from .conftest import needs_ffmpeg


def _app(dry_settings, take_series):
    take_series.caption.credits = "Animated with {model} on Higgsfield"
    app = build(dry_settings, take_series)
    app.pipeline.propose_cast()
    app.pipeline.approve_cast("Kai", 0)
    return app


def _calls(p, kind):
    return [c for k, c in p.media.calls if k == kind]


@needs_ffmpeg
def test_one_take_cycle(dry_settings, take_series):
    app = _app(dry_settings, take_series)
    p = app.pipeline
    job = p.plan_next()
    out = p.generate(job)

    assert probe_duration(out) == pytest.approx(15, abs=0.1) and has_audio(out)
    assert app.db.get_job(job)["status"] == JobStatus.AWAITING_REVIEW

    # one generation for the whole reel, starting from the composed opening frame
    takes = _calls(p, "take")
    keyframe = app.db.kv_get(f"keyframe:{job}")
    assert len(takes) == 1 and takes[0]["model"] == "kling3" and takes[0]["start_image"] == keyframe
    assert takes[0]["duration"] == 15 and "s | TWIST] Cut to:" in takes[0]["prompt"]
    assert [x["duration"] for x in takes[0]["shots"]] == [s.seconds for s in app.db.plan(job).shots]
    assert "[0-" not in takes[0]["prompt_global"]  # Kling gets the shot list via multi_prompt
    assert not _calls(p, "video")  # no per-shot calls in v3

    # the opening frame is painted from the approved sheet + the location plate
    kf_call = next(c for c in _calls(p, "image") if c["prompt"].startswith("Opening frame"))
    assert kf_call["image_urls"] == [p.cast_urls()["Kai"], app.db.kv_get(f"plate:{job}")]

    with Image.open(app.db.kv_get(f"cover:{job}")) as im:
        assert im.format == "JPEG" and im.size == (1080, 1920)
    assert "Animated with Kling 3.0 on Higgsfield" in app.db.get_job(job)["caption"]
    assert p.notifier.sent[-1].startswith("VIDEO") and "Kling 3.0" in p.notifier.sent[-1]

    p.retake(job)
    assert app.db.kv_get(f"take_attempt:{job}") == "2" and len(_calls(p, "take")) == 2
    assert len([c for c in _calls(p, "image") if c["prompt"].startswith("Opening frame")]) == 1  # reused

    old_music = app.db.kv_get(f"music_path:{job}")
    p.regenerate_music(job)
    assert app.db.kv_get(f"music_path:{job}") != old_music

    with pytest.raises(ValueError, match="one take"):
        p.regenerate_shot(job, 0)


@needs_ffmpeg
def test_bakeoff_shares_everything_but_the_model(dry_settings, take_series):
    app = _app(dry_settings, take_series)
    p = app.pipeline
    ids = p.bakeoff()
    jobs = [app.db.get_job(i) for i in ids]
    assert [j["status"] for j in jobs] == [JobStatus.AWAITING_REVIEW] * 2
    assert jobs[0]["episode_no"] == jobs[1]["episode_no"] == 1

    takes = _calls(p, "take")
    assert [t["model"] for t in takes] == ["kling3", "seedance2"]
    # Kling starts from the composed opening frame; the reference model gets plate + sheets
    keyframe = app.db.kv_get(f"keyframe:{ids[0]}")
    assert takes[0]["start_image"] == keyframe and "image 1" not in takes[0]["prompt"]
    plate = app.db.kv_get(f"plate:{ids[0]}")
    assert takes[1]["image_urls"] == [plate, p.cast_urls()["Kai"]]
    assert takes[1]["prompt"].startswith("Use image 1 as the location reference, image 2 as the Kai")
    assert app.db.kv_get(f"plate:{ids[1]}") == plate
    # paid once: one plate, one opening frame, one track (same bytes) for both models
    image_prompts = [
        c["prompt"] for c in _calls(p, "image") if "Character reference sheet" not in c["prompt"]
    ]
    assert len(image_prompts) == 2
    music = [Path(app.db.kv_get(f"music_path:{i}")).read_bytes() for i in ids]
    assert music[0] == music[1]
    assert "Seedance 2.0" in jobs[1]["caption"] and "Kling 3.0" in jobs[0]["caption"]

    name, rejected = p.pick_winner(ids[1])
    assert name == "seedance2" and rejected == [ids[0]]
    assert app.db.get_job(ids[0])["status"] == JobStatus.REJECTED
    nxt = p.plan_next()
    assert p.take_model_for(nxt) == "seedance2"  # every reel from now on


def test_take_keyboard_offers_retake_and_winner():
    kb = take_keyboard(4, music_regen=True, winner_label="Kling 3.0")["inline_keyboard"]
    data = [b["callback_data"] for row in kb for b in row]
    assert data == ["a:4", "x:4", "t:4", "m:4", "w:4"]
    assert kb[-1][0]["text"] == "🏆 Use Kling 3.0 from now on"
    assert len(take_keyboard(4, music_regen=False)["inline_keyboard"]) == 2


@needs_ffmpeg
@pytest.mark.parametrize("with_sfx", [True, False])
def test_take_mix_with_and_without_model_audio(tmp_path, with_sfx):
    take = tmp_path / "take.mp4"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=720x1280:rate=24:d=6"]
    if with_sfx:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=900:d=6", "-shortest"]
    subprocess.run([*cmd, "-pix_fmt", "yuv420p", str(take)], check=True)
    music = tmp_path / "music.m4a"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=300:d=8", str(music)],
        check=True,
    )
    out = assemble_take(
        TakeAssembly(
            take,
            6.0,
            music,
            0.0,
            -8.0,
            [(2.2, 3.0), (4.0, 4.6)],
            tmp_path / "final.mp4",
            speech=[(0.0, 1.5)],
            flashes=[3.0],
        )
    )
    assert has_audio(take) is with_sfx
    assert probe_duration(out) == pytest.approx(6, abs=0.1) and has_audio(out)
    w, h = (
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0",
                str(out),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        .stdout.strip()
        .split(",")
    )
    assert (int(w), int(h)) == (1080, 1920)


@needs_ffmpeg
def test_bookend_dialogue_gets_subtitles_and_a_speech_aware_mix(dry_settings, take_series):
    take_series.take.dialogue = "bookends"
    app = _app(dry_settings, take_series)
    p = app.pipeline
    job = p.plan_next()
    plan = app.db.plan(job)
    spoken = [i for i, s in enumerate(plan.shots) if s.line]
    assert spoken == [0, len(plan.shots) - 1]
    out = p.generate(job)
    assert probe_duration(out) == pytest.approx(15, abs=0.1) and has_audio(out)
    d = Path(app.db.get_job(job)["video_path"]).parent
    assert sorted(x.name for x in d.glob("sub_*.png")) == [f"sub_{i}.png" for i in spoken]
    assert 'says: "Ten seconds. Easy."' in app.db.kv_get(f"prompt:{job}:take")


def test_snap_moves_planned_cut_to_the_real_one():
    from reelbot.assemble import snap

    assert snap(9.0, [2.1, 8.6, 12.0]) == 8.6
    assert snap(9.0, [2.1, 12.0]) == 9.0  # nothing near: keep the plan


@needs_ffmpeg
def test_reference_video_goes_only_to_models_that_take_one(dry_settings, take_series):
    take_series.models.takes["seedance2"].input["video_urls"] = "{video_urls}"
    take_series.episodes[0].reference_video = "https://example.com/clip.mp4"
    app = _app(dry_settings, take_series)
    app.pipeline.bakeoff()
    kling, seed = _calls(app.pipeline, "take")
    assert kling["video_urls"] == [] and not kling["prompt"].startswith("Use video 1")
    assert seed["video_urls"] == ["https://example.com/clip.mp4"]
    assert seed["prompt"].startswith("Use video 1 as the motion, timing and camera reference only")


def test_rejected_drafts_do_not_count_as_used_premises(db):
    from reelbot.models import EpisodePlan

    plan = EpisodePlan.model_validate(
        {
            "title": "metro dash",
            "category": "Last Metro",
            "logline": "l",
            "hook": "h",
            "music_prompt": "m",
            "location": {"name": "platform", "plate_prompt": "an empty metro platform at night"},
            "shots": [
                {
                    "beat": "hook",
                    "size": "WS",
                    "angle": "eye level",
                    "camera": "static",
                    "action": "the doors begin to close",
                    "light": "cold key",
                }
            ],
        }
    )
    a, b = db.create_job(1, plan), db.create_job(1, plan)
    db.update_job(a, status=JobStatus.REJECTED)
    db.update_job(b, status=JobStatus.PUBLISHED)
    assert db.past_titles() == ["METRO DASH (Last Metro)"]
