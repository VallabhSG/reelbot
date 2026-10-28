"""v3 one-take: storyboard rules, prompt composition, config validation."""

import pytest

from reelbot.models import BEATS, EpisodePlan
from reelbot.planner import Planner
from reelbot.storyboard import (
    compose_keyframe_prompt,
    compose_take_prompt,
    default_beats,
    fit_seconds,
    normalise,
    shot_starts,
    storyboard_problems,
)

from .conftest import make_take_series


def _shot(beat, size, seconds=2, cast=("Kai",), ends="Kai lands in a crouch"):
    return {
        "beat": beat,
        "size": size,
        "seconds": seconds,
        "angle": "low angle",
        "camera": "slow push-in",
        "cast": list(cast),
        "action": "Kai vaults the closing gate",
        "ends": ends,
        "sound": "sneakers squeak on wet tiles",
        "light": "cold key from screen-left, 5600K",
    }


def _plan(sizes=("WS", "MCU", "ECU", "MS", "CU", "WS"), seconds=(2, 2, 2, 2, 4, 3), **over):
    beats = default_beats(len(sizes))
    base = {
        "title": "gate",
        "category": "Metro",
        "logline": "One gate, one second left.",
        "hook": "The gate always closes one step early.",
        "location": {"name": "metro ticket hall", "plate_prompt": "an empty metro ticket hall at night"},
        "winning_move": "Kai vaults the gate as it closes",
        "direction": "left to right",
        "motif": "a red hair tie",
        "opening_frame": "Kai stands at the far left, ticket in his teeth, gates ahead",
        "shots": [_shot(b, s, sec) for b, s, sec in zip(beats, sizes, seconds, strict=True)],
        "music_prompt": "driving drum and bass, instrumental",
    }
    base.update(over)
    return EpisodePlan.model_validate(base)


def test_default_beats_cover_every_beat_in_order():
    assert default_beats(5) == list(BEATS)
    assert default_beats(6) == ["hook", "escalate", "clash", "clash", "climax", "twist"]
    seven = default_beats(7)
    assert len(seven) == 7 and set(seven) == set(BEATS) and seven == sorted(seven, key=BEATS.index)


@pytest.mark.parametrize(
    "seconds, total",
    [([2, 2, 2, 2, 4, 3], 15), ([5, 5, 5, 5, 5, 5], 15), ([1, 1, 1, 1, 1, 1], 15), ([3, 1, 1, 1, 1, 9], 15)],
)
def test_fit_seconds_sums_exactly_within_bounds(seconds, total):
    out = fit_seconds(seconds, total, 1, 4)
    assert sum(out) == total and all(1 <= s <= 4 for s in out)


def test_valid_take_plan_passes_and_is_fitted_to_the_take(take_series):
    plan = _plan(seconds=(3, 3, 3, 3, 3, 3))
    assert storyboard_problems(take_series, plan) == []
    plan = normalise(take_series, plan)
    assert sum(s.seconds for s in plan.shots) == 15
    assert shot_starts(plan)[0] == 0 and shot_starts(plan)[-1] == 15 - plan.shots[-1].seconds


@pytest.mark.parametrize(
    "over, expected",
    [
        ({"motif": ""}, "motif is required"),
        ({"opening_frame": ""}, "opening_frame is required"),
        ({"direction": ""}, "direction is required"),
        ({"winning_move": ""}, "winning_move is required"),
    ],
)
def test_take_continuity_fields_are_required(take_series, over, expected):
    assert any(expected in p for p in storyboard_problems(take_series, _plan(**over)))


def test_every_shot_but_the_last_needs_an_end_state(take_series):
    plan = _plan()
    plan.shots[2].ends = ""
    plan.shots[-1].ends = ""  # the last shot may end however it likes
    problems = storyboard_problems(take_series, plan)
    assert problems == ["shot 3 needs 'ends': the exact visible state at the cut"]


def test_beats_may_repeat_but_never_go_backwards(take_series):
    plan = _plan()
    plan.shots[3].beat = "escalate"  # hook, escalate, clash, ESCALATE, climax, twist
    assert any("never go backwards" in p for p in storyboard_problems(take_series, plan))


def test_reference_prompt_maps_location_then_cast_and_lists_timed_cuts(take_series):
    plan = normalise(take_series, _plan())
    tp = compose_take_prompt(take_series, plan, "image {n}", start_frame=False)
    assert tp.roles == ["location", "Kai"]
    assert tp.prompt.startswith("Use image 1 as the location reference, image 2 as the Kai reference.")
    assert "[CHARACTER: Kai] (reference image 2) Keep: Kai, a lanky courier" in tp.prompt
    assert "[LOCATION] (reference image 1) an empty metro ticket hall at night." in tp.prompt
    lines = [ln for ln in tp.prompt.splitlines() if ln.startswith("[") and "s |" in ln]
    assert lines[0].startswith("[0-2s | HOOK] Wide shot") and lines[1].startswith("[2-4s | ESCALATE] Cut to:")
    assert "Screen direction: left to right" in tp.prompt and "Recurring detail: a red hair tie" in tp.prompt
    assert "Ends on: Kai lands in a crouch" in lines[0] and "Ends on" not in lines[-1]  # last shot
    assert "No speech, no dialogue, no narration, no music" in tp.prompt
    assert [s["duration"] for s in tp.shots] == [s.seconds for s in plan.shots]
    assert "[0-2s" not in tp.global_prompt and "[CHARACTER: Kai]" in tp.global_prompt


def test_start_frame_prompt_has_no_reference_mapping(take_series):
    plan = normalise(take_series, _plan())
    tp = compose_take_prompt(take_series, plan, "", start_frame=True)
    assert tp.roles == ["opening", "Kai"] and "image 1" not in tp.prompt
    assert tp.prompt.startswith("Shot 1 starts exactly on the given first frame.")
    assert all(len(s["prompt"]) <= 500 for s in tp.shots)


def test_spoken_lines_are_quoted_and_audio_allows_them(take_series):
    plan = _plan()
    plan.shots[0].line, plan.shots[0].speaker = "Ten seconds. Easy.", "Kai"
    tp = compose_take_prompt(take_series, normalise(take_series, plan), "", start_frame=True)
    assert 'Kai says: "Ten seconds. Easy."' in tp.prompt
    assert "the quoted lines are spoken by the named character, lip-synced" in tp.prompt


def test_keyframe_prompt_integrates_cast_into_the_scene(take_series):
    p = compose_keyframe_prompt(take_series, _plan(), ["Kai"])
    assert "image 1 is Kai" in p and "image 2 is the location" in p
    assert "no cut-out look" in p and "Kai stands at the far left" in p


def test_planner_uses_the_take_prompt_and_shot_count(take_series):
    planner = Planner(None, take_series)
    sp = planner.system_prompt()
    assert "ONE continuous 15-second scene in exactly 6 shots" in sp and '"ends": str' in sp
    assert "SHOT_COUNT_JSON=6" in planner.user_prompt(1, [])


def test_series_validation_for_take_mode():
    s = make_take_series()
    assert s.estimate_reel_cost("kling3") == pytest.approx(2 * 0.04 + 15 * 0.12)
    # reference-to-video needs no opening frame: one image (the plate) instead of two
    assert s.estimate_reel_cost("seedance2") == pytest.approx(1 * 0.04 + 15 * 0.25)
    with pytest.raises(ValueError, match="not in models.takes"):
        make_take_series(model="veo9")
    with pytest.raises(ValueError, match="can't fill"):
        make_take_series(seconds=30, shots=6, max_shot_s=4)


def test_prompt_shrinks_to_the_model_limit_without_losing_shots(take_series):
    plan = normalise(take_series, _plan())
    full = compose_take_prompt(take_series, plan, "", start_frame=True).prompt
    assert "Sound:" in full and full.count("[LIGHTING]") == 1  # one scene light, stated once
    for limit in (len(full) - 50, len(full) - 400, 900):
        p = compose_take_prompt(take_series, plan, "", start_frame=True, max_chars=limit).prompt
        assert len(p) <= limit and all(f"| {b.upper()}]" in p for b in {"hook", "climax", "twist"})
    p = compose_take_prompt(take_series, plan, "", start_frame=True, max_chars=len(full) - 50).prompt
    assert "Sound:" not in p and "Ends on:" in p  # sounds go first, end states stay


def _talk_series():
    from reelbot.series import Character

    s = make_take_series(dialogue="bookends", max_lines=2)
    s.characters.append(Character(name="Mochi", description="a chubby orange cat", speaks=False))
    return s


def test_wordless_series_rejects_lines(take_series):
    plan = _plan()
    plan.shots[0].line, plan.shots[0].speaker = "Go!", "Kai"
    assert "this series is wordless: leave every 'line' empty" in storyboard_problems(take_series, plan)


@pytest.mark.parametrize(
    "shot, line, speaker, cast, expected",
    [
        (2, "Now!", "Kai", ["Kai"], "lines only in hook/escalate (set-up) or twist (punchline)"),
        (0, "one two three four five six seven eight nine ten eleven", "Kai", ["Kai"], "max 10 words"),
        (0, "Meow?", "Mochi", ["Kai", "Mochi"], "speaker must be a character in that shot who can talk"),
        (0, "Hey.", "Kai", ["Mochi"], "speaker must be a character in that shot who can talk"),
    ],
)
def test_bookend_dialogue_rules(shot, line, speaker, cast, expected):
    s = _talk_series()
    plan = _plan()
    plan.shots[shot].line, plan.shots[shot].speaker, plan.shots[shot].cast = line, speaker, cast
    assert any(expected in p for p in storyboard_problems(s, plan))


def test_bookends_valid_and_capped():
    s = _talk_series()
    plan = _plan()
    plan.shots[0].line, plan.shots[0].speaker = "Drive me. I'm late.", "kai"
    plan.shots[-1].line, plan.shots[-1].speaker = "Wrong train.", "Kai"
    assert storyboard_problems(s, plan) == []
    assert normalise(s, plan).shots[0].speaker == "Kai"
    plan.shots[1].line, plan.shots[1].speaker = "Hold the door!", "Kai"
    assert "at most 2 spoken lines per episode, got 3" in storyboard_problems(s, plan)


def test_planner_explains_bookends_and_who_cannot_talk():
    sp = Planner(None, _talk_series()).system_prompt()
    assert "DIALOGUE BOOKENDS" in sp and "who can talk: Kai" in sp and "Mochi never talks" in sp
    assert '"line": str, "speaker": str' in sp
