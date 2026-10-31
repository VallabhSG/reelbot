"""v3 one-take: storyboard rules, prompt composition, config validation."""

import re

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
    lines = [ln for ln in tp.prompt.splitlines() if re.match(r"Shot \d+ \(", ln)]
    assert lines[0].startswith("Shot 1 (2 s, HOOK): Wide shot")
    assert lines[1].startswith("Shot 2 (2 s, ESCALATE): Hard cut. ")
    assert "Screen direction: left to right" in tp.prompt and "Recurring detail: a red hair tie" in tp.prompt
    assert "Ends on: Kai lands in a crouch" in lines[0] and "Ends on" not in lines[-1]  # last shot
    assert "No speech, no dialogue, no narration, no music" in tp.prompt
    assert [s["duration"] for s in tp.shots] == [s.seconds for s in plan.shots]
    assert "Shot 1 (" not in tp.global_prompt and "[CHARACTER: Kai]" in tp.global_prompt


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
        assert len(p) <= limit and all(f", {b.upper()}):" in p for b in {"hook", "climax", "twist"})
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


def test_planner_asks_for_relatable_stakes_and_physical_spectacle(take_series):
    sp = Planner(None, take_series).system_prompt()
    assert "The stake is social and relatable" in sp and "Make the feeling PHYSICAL" in sp


class ScriptedLLM:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def json_chat(self, system, user, temperature=0.9):
        self.calls.append((system, user))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _raw_plan():
    return _plan().model_dump()


CLEAR = {"summary": "she makes it", "confusions": [], "clear": True, "score": 9}
FOGGY = {
    "summary": "she does something to the door?",
    "confusions": ["what is the red coin"],
    "clear": False,
    "score": 4,
}


def test_scripted_episode_is_fixed_and_homage_never_named():
    from reelbot.series import EpisodeSeed

    s = make_take_series()
    s.episodes[0] = EpisodeSeed(
        category="Gate",
        stake="the gate starts closing",
        move="Kai vaults it",
        payoff="he lands in a puddle",
        homage="a spaghetti-western standoff",
    )
    up = Planner(None, s).user_prompt(1, [])
    assert "FIXED STORY: follow it exactly" in up and "- Winning move (climax): Kai vaults it" in up
    assert "stage it like a spaghetti-western standoff" in up and "Never name the film" in up


def test_unclear_storyboard_goes_back_with_the_viewers_confusion():
    llm = ScriptedLLM(_raw_plan(), FOGGY, _raw_plan(), CLEAR)
    Planner(llm, make_take_series()).plan(1, [])
    critic_system, critic_user = llm.calls[1]
    assert "FIRST-TIME VIEWER" in critic_system and "SHOTS:" in critic_user
    assert "winning_move" not in critic_user  # the viewer sees only the pictures
    retry = llm.calls[2][1]
    assert "what is the red coin" in retry and "visible with the sound off" in retry


def test_critic_never_blocks_the_day():
    # every storyboard foggy: ship the clearest one instead of failing
    foggy = [FOGGY, {**FOGGY, "score": 6}, FOGGY, FOGGY, FOGGY, FOGGY]
    llm = ScriptedLLM(*[x for f in foggy for x in (_raw_plan(), f)])
    assert Planner(llm, make_take_series()).plan(1, []).title == "GATE"
    # critic down: accept the valid storyboard
    llm = ScriptedLLM(_raw_plan(), RuntimeError("groq 503"))
    assert Planner(llm, make_take_series()).plan(1, []).title == "GATE"


def test_unscripted_episode_picks_a_famous_movie_moment():
    s = make_take_series()
    s.homages = ["a western standoff", "bullet time"]
    s.episodes = []
    up = Planner(None, s).user_prompt(3, [])
    assert "pick the ONE famous movie moment" in up and "a western standoff; bullet time" in up


def test_near_miss_shot_sizes_and_beats_are_accepted():
    from reelbot.models import ShotPlan

    base = {"angle": "low angle", "camera": "slow push-in", "action": "Juno grabs the cable", "light": "warm"}
    for size, want in [
        ("extreme close-up", "ECU"),
        ("Close Up", "CU"),
        ("medium shot", "MS"),
        ("ecu", "ECU"),
    ]:
        assert ShotPlan(beat="hook", size=size, **base).size == want
    for beat, want in [("Escalation", "escalate"), ("payoff", "twist"), ("CLIMAX", "climax")]:
        assert ShotPlan(beat=beat, size="WS", **base).beat == want
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ShotPlan(beat="hook", size="dutch tilt", **base)


def test_a_missing_music_prompt_does_not_sink_the_plan():
    from reelbot.models import EpisodePlan

    raw = {
        "title": "low battery",
        "category": "2% Battery",
        "logline": "l",
        "hook": "h",
        "location": {"name": "living room", "plate_prompt": "a cosy living room in the evening, sofa"},
        "shots": [
            {
                "beat": "hook",
                "size": "WS",
                "angle": "eye level",
                "camera": "static",
                "action": "the phone dies",
                "light": "warm",
            }
        ],
    }
    assert "anime" in EpisodePlan.model_validate(raw).music_prompt
    assert EpisodePlan.model_validate({**raw, "music": "lofi drums"}).music_prompt == "lofi drums"


def _directed_plan():
    shots = []
    for b, s, sec in zip(
        default_beats(6), ("WS", "MCU", "ECU", "MS", "CU", "WS"), (2, 2, 2, 2, 4, 3), strict=True
    ):
        shot = _shot(b, s, sec)
        shot |= {
            "framing": "Kai lower-left third facing right, the gate upper right",
            "performance": "eyes snap wide, jaw set, jacket hem flapping",
            "fx": "speed lines" if b == "clash" else "",
        }
        shots.append(shot)
    return _plan(
        layout="the gates stand on the RIGHT, the ticket machine on the LEFT wall, three metres apart",
        shots=shots,
    )


def test_director_prompt_restates_the_look_in_every_shot_and_locks_the_wardrobe(take_series):
    take_series.characters[0].look = "spiky black hair, red track jacket, grey joggers"
    plan = normalise(take_series, _directed_plan())
    p = compose_take_prompt(take_series, plan, "", start_frame=True).prompt
    shot_lines = [ln for ln in p.splitlines() if re.match(r"Shot \d+ \(", ln)]
    assert len(shot_lines) == len(plan.shots)
    assert all("Kai (spiky black hair, red track jacket, grey joggers, unchanged)" in ln for ln in shot_lines)
    assert all("Framing: Kai lower-left third" in ln and "Acting: eyes snap wide" in ln for ln in shot_lines)
    assert "Effect: speed lines" in p
    assert "[LAYOUT, fixed in every shot] the gates stand on the RIGHT" in p
    assert "[LOCKS] Wardrobe and look, identical in every shot: Kai: spiky black hair" in p
    assert "Nobody takes off, adds or changes any clothing" in p


def test_director_prompt_trims_acting_before_end_states_and_never_framing(take_series):
    take_series.characters[0].look = "spiky black hair, red track jacket, grey joggers"
    plan = normalise(take_series, _directed_plan())
    full = compose_take_prompt(take_series, plan, "", start_frame=True).prompt
    no_sound = full.replace(" Sound: sneakers squeak on wet tiles.", "")
    p = compose_take_prompt(take_series, plan, "", start_frame=True, max_chars=len(no_sound) - 10).prompt
    assert "Sound:" not in p and "Acting:" not in p and "Ends on:" in p and "Framing:" in p
    tight = compose_take_prompt(take_series, plan, "", start_frame=True, max_chars=1500).prompt
    assert len(tight) <= 1500 and "Framing:" in tight


@pytest.mark.parametrize(
    "wrap",
    [
        lambda raw: {**{k: v for k, v in raw.items() if k != "shots"}, "storyboard": raw["shots"]},
        lambda raw: {**{k: v for k, v in raw.items() if k != "shots"}, "shot_list": raw["shots"]},
        lambda raw: {"episode": raw},
        lambda raw: {**raw, "shots": {str(i + 1): s for i, s in enumerate(raw["shots"])}},
    ],
)
def test_shots_under_another_name_still_make_a_plan(wrap):
    raw = _raw_plan()
    plan = EpisodePlan.model_validate(wrap(raw))
    assert len(plan.shots) == len(raw["shots"])


def test_a_plan_without_shots_gets_told_where_they_go(take_series):
    raw = {k: v for k, v in _raw_plan().items() if k != "shots"}

    class LLM:
        def __init__(self):
            self.users = []

        def json_chat(self, system, user, temperature=0.9):
            self.users.append(user)
            return raw

    llm = LLM()
    with pytest.raises(RuntimeError):
        Planner(llm, take_series).plan(1, [])
    assert "THIS ANSWER: every field EXCEPT the shots" in llm.users[0]
    assert "THE EPISODE BELOW IS GOOD" in llm.users[1]  # the shots, asked for on their own
    assert 'Your JSON had no "shots" array' in llm.users[2]


def test_the_episode_and_its_shots_are_two_answers():
    raw = _raw_plan()
    episode = {k: v for k, v in raw.items() if k != "shots"}
    llm = ScriptedLLM(episode, {"shots": raw["shots"]}, CLEAR)
    plan = Planner(llm, make_take_series()).plan(1, [])
    assert len(plan.shots) == len(raw["shots"]) and plan.logline == raw["logline"]
    assert "THIS ANSWER: every field EXCEPT the shots" in llm.calls[0][1]
    assert "THE EPISODE BELOW IS GOOD" in llm.calls[1][1] and raw["logline"] in llm.calls[1][1]


def test_a_cut_off_answer_is_asked_again():
    from reelbot.providers.llm import AnswerCutOff

    raw = _raw_plan()
    episode = {k: v for k, v in raw.items() if k != "shots"}
    cut = AnswerCutOff("the model's JSON was incomplete (5000 characters came back)")
    llm = ScriptedLLM(episode, cut, {"shots": raw["shots"]}, CLEAR)
    plan = Planner(llm, make_take_series()).plan(1, [])
    assert len(plan.shots) == len(raw["shots"])
    third = llm.calls[2][1]  # the episode survived: only the shots are asked for again
    assert "THE EPISODE BELOW IS GOOD" in third and "was cut off before the end" in third


def test_only_the_shots_are_asked_again_when_the_episode_is_good():
    raw = _raw_plan()
    short = {**raw, "shots": raw["shots"][:5], "logline": "Kept from the first answer."}
    llm = ScriptedLLM(short, {"shots": raw["shots"]}, CLEAR)
    plan = Planner(llm, make_take_series()).plan(1, [])
    assert plan.logline == "Kept from the first answer." and len(plan.shots) == len(raw["shots"])
    second = llm.calls[1][1]
    assert "THE EPISODE BELOW IS GOOD" in second and "Kept from the first answer." in second
    assert "exactly 6 shots, got 5" in second


def test_the_problem_starts_unsolved_and_examples_are_not_copied(take_series):
    sp = Planner(None, take_series).system_prompt()
    assert "THE PROBLEM IS STILL UNSOLVED" in sp and '"start_state": str' in sp
    assert "never copy their objects" in sp and "never as a poster or wall decoration" in sp


def test_the_critic_sees_the_place_and_the_start(take_series):
    raw = {**_raw_plan(), "start_state": "the charger is already plugged in by the sofa"}
    llm = ScriptedLLM(raw, CLEAR)
    Planner(llm, take_series).plan(1, [])
    system, user = llm.calls[-1]
    assert "the problem already solved at the" in system
    assert "AT THE START: the charger is already plugged in by the sofa" in user and "THE PLACE:" in user
