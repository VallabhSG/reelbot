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
    assert takes[0]["duration"] == 15 and ", TWIST): Hard cut." in takes[0]["prompt"]
    assert [x["duration"] for x in takes[0]["shots"]] == [s.seconds for s in app.db.plan(job).shots]
    assert "Shot 1 (" not in takes[0]["prompt_global"]  # Kling gets the shot list via multi_prompt
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


@needs_ffmpeg
def test_impact_frame_goes_black_and_white_under_speed_lines_and_keeps_subtitles(tmp_path):
    from PIL import Image

    from reelbot.assemble import render_speed_lines
    from reelbot.titlecard import render_subtitle

    take = tmp_path / "take.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=720x1280:rate=30:d=4"]
        + ["-pix_fmt", "yuv420p", str(take)],
        check=True,
    )
    sub = render_subtitle("Two minutes!", None, tmp_path / "sub.png")
    out = assemble_take(
        TakeAssembly(
            take,
            4.0,
            None,
            0.0,
            -8.0,
            [],
            tmp_path / "final.mp4",
            subtitles=[(sub, 0.2, 1.5)],
            flashes=[2.0],
            impacts=[(2.07, 2.27)],
            speed_lines=render_speed_lines(tmp_path / "lines.png"),
        )
    )

    def frame(t):
        png = tmp_path / f"f{t}.png"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(t), "-i", str(out), "-frames:v", "1", str(png)],
            check=True,
        )
        return Image.open(png).convert("RGB")

    def pixels(im):
        small = im.resize((60, 100))
        return [small.getpixel((x, y)) for x in range(60) for y in range(100)]

    def colourful(im):
        return sum(max(px) - min(px) > 40 for px in pixels(im))

    assert colourful(frame(1.0)) > 500  # normal colour picture
    impact = frame(2.15)
    assert colourful(impact) < 50  # black & white
    assert sum(min(px) > 230 for px in pixels(impact)) > 100  # white speed lines across the frame
    assert colourful(frame(3.0)) > 500  # and back to colour


@needs_ffmpeg
def test_music_ducks_only_while_the_words_are_spoken(dry_settings, take_series, monkeypatch):
    from reelbot import pipeline as pl

    take_series.take.dialogue = "bookends"
    app = _app(dry_settings, take_series)
    seen = []
    real = pl.assemble_take
    monkeypatch.setattr(pl, "assemble_take", lambda p: (seen.append(p), real(p))[1])
    job = app.pipeline.plan_next()
    app.pipeline.generate(job)
    plan = app.db.plan(job)
    last = plan.shots[-1]
    a, b = seen[-1].speech[-1]
    assert b - a == pytest.approx(pl.spoken_seconds(last.line))  # "Wrong train." ~1.8 s
    assert b - a < last.seconds  # the rest of the payoff shot keeps its music
    assert seen[-1].impacts and seen[-1].speed_lines.exists()


@needs_ffmpeg
def test_the_boss_gets_a_name_card_over_the_opening(dry_settings, take_series):
    app = _app(dry_settings, take_series)
    p = app.pipeline
    job = p.plan_next()
    out = p.generate(job)
    card = Path(app.db.get_job(job)["video_path"]).parent / "boss_card.png"
    assert card.exists() and probe_duration(out) == pytest.approx(15, abs=0.1)
    take_series.take.boss_card_s = 0
    job2 = p.plan_next()
    p.generate(job2)
    assert not (Path(app.db.get_job(job2)["video_path"]).parent / "boss_card.png").exists()


def test_every_other_episode_ends_in_a_triumph(take_series):
    from reelbot.planner import ENDING_DEADPAN, ENDING_TRIUMPH, Planner

    take_series.episodes = []  # unscripted episodes (a scripted one keeps its own payoff)
    planner = Planner(None, take_series)
    assert ENDING_DEADPAN in planner.user_prompt(1, []) and ENDING_TRIUMPH in planner.user_prompt(2, [])
    take_series.take.endings = "deadpan"
    assert ENDING_TRIUMPH not in planner.user_prompt(2, [])
    assert "COLOUR CODE THE FIGHT" in planner.system_prompt()


@needs_ffmpeg
def test_a_failed_take_falls_back_to_the_backup_model(dry_settings, take_series):
    take_series.models.takes["kling3"].fallback = "seedance2"
    app = _app(dry_settings, take_series)
    p = app.pipeline
    real = p.media.take

    def flaky(name, ctx, job_id):
        if name == "kling3":
            raise RuntimeError("provider down")
        return real(name, ctx, job_id)

    p.media.take = flaky
    job = p.plan_next()
    out = p.generate(job)
    assert out.exists()
    assert any("provider down" in m and "Seedance 2.0" in m for m in p.notifier.sent)


def test_budget_stops_are_never_retried_on_the_fallback(dry_settings, take_series):
    from reelbot.budget import BudgetExceeded

    take_series.models.takes["kling3"].fallback = "seedance2"
    app = _app(dry_settings, take_series)
    calls = []

    def broke(name, ctx, job_id):
        calls.append(name)
        raise BudgetExceeded("cap")

    app.pipeline.media.take = broke
    with pytest.raises(BudgetExceeded):
        app.pipeline._take_with_fallback("kling3", {}, 1)
    assert calls == ["kling3"]


@needs_ffmpeg
def test_the_prompt_fits_the_fallback_models_limit_too(dry_settings, take_series):
    take_series.models.takes["kling3"].fallback = "seedance2"
    take_series.models.takes["kling3"].max_prompt_chars = 0
    take_series.models.takes["seedance2"].max_prompt_chars = 900
    app = _app(dry_settings, take_series)
    p = app.pipeline
    real, prompts = p.media.take, []

    def spy(name, ctx, job_id):
        prompts.append(ctx["prompt"])
        return real(name, ctx, job_id)

    p.media.take = spy
    p.generate(p.plan_next())
    assert prompts and all(len(x) <= 900 for x in prompts)


@needs_ffmpeg
def test_the_director_writes_every_shot_out_before_any_image_is_made(dry_settings, take_series):
    app = _app(dry_settings, take_series)
    p = app.pipeline
    job = p.plan_next()
    p.generate(job)
    plan = app.db.plan(job)  # saved: a retake or resume reuses the same write-up
    assert plan.world.startswith("Cold white tiles") and plan.blocking
    assert all(s.detail.startswith(f"Shot {i}:") for i, s in enumerate(plan.shots, 1))
    images = [c["prompt"] for c in _calls(p, "image")]
    assert any("Empty environment" in x and "Cold white tiles" in x for x in images)  # the plate
    assert any(x.startswith("Opening frame") and "The set: Cold white tiles" in x for x in images)
    take = _calls(p, "take")[0]["prompt"]
    assert "[SET, every element fixed in every shot] Cold white tiles" in take
    assert "a 35 mm lens at waist height" in take


@needs_ffmpeg
def test_a_failed_director_pass_never_blocks_the_reel(dry_settings, take_series):
    app = _app(dry_settings, take_series)
    p = app.pipeline

    def down(plan):
        raise RuntimeError("LLM down")

    p.director.direct = down
    job = p.plan_next()
    assert p.generate(job).exists()
    assert any("director pass failed" in m for m in p.notifier.sent)
    assert not app.db.plan(job).world


def test_the_director_can_be_switched_off(dry_settings, take_series):
    take_series.take.director = False
    app = _app(dry_settings, take_series)
    plan = app.db.plan(app.pipeline.plan_next())
    assert app.pipeline._directed(plan) is plan
