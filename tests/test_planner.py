import pytest

from reelbot.models import BEATS, EpisodePlan
from reelbot.planner import Planner


def _shot(beat, size, cast=("Kai",), action="Kai vaults the closing gate and lands in a crouch"):
    return {
        "beat": beat,
        "size": size,
        "angle": "low angle",
        "camera": "slow push-in",
        "cast": list(cast),
        "action": action,
        "light": "cold key from screen-left, 5600K",
    }


def _plan(sizes=("WS", "MCU", "MS", "ECU", "MS"), beats=BEATS, **over):
    base = {
        "title": "divebomb",
        "category": "Kart",
        "emoji": "🏎️",
        "logline": "Two karts, one corner.",
        "hook": "Same move, different price.",
        "location": {
            "name": "hairpin kart track",
            "plate_prompt": "an empty hairpin kart track at dusk, tyre walls",
        },
        "shots": [_shot(b, s) for b, s in zip(beats, sizes, strict=False)],
        "music_prompt": "driving synthwave, instrumental",
    }
    base.update(over)
    return base


class ScriptedLLM:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.users = []

    def json_chat(self, system, user, temperature=0.9):
        self.users.append(user)
        return self.responses.pop(0)


def test_valid_plan_passes_and_title_is_upper_cased(series):
    plan = Planner(ScriptedLLM(_plan()), series).plan(5, [])
    assert isinstance(plan, EpisodePlan) and plan.title == "DIVEBOMB"
    assert [s.beat for s in plan.shots] == list(BEATS)


def test_schema_errors_are_fed_back(series):
    llm = ScriptedLLM({"title": "X"}, _plan())
    Planner(llm, series).plan(5, [])
    assert "broke these rules" in llm.users[1] and "location" in llm.users[1]


@pytest.mark.parametrize(
    "bad, expected",
    [
        (_plan(sizes=("WS", "WS", "MS", "ECU", "MS")), "consecutive shots need different sizes"),
        (_plan(sizes=("WS", "MCU", "MS", "MCU", "MS")), "at least one CU or ECU"),
        (_plan(sizes=("MS", "MCU", "MS", "ECU", "MS")), "at least one WS or EWS"),
        (
            _plan(beats=("hook", "clash", "escalate", "climax", "twist")),
            "beats must be exactly in this order",
        ),
        (_plan(sizes=("WS", "MCU", "MS", "ECU")), "exactly 5 shots"),
    ],
)
def test_storyboard_rules_are_enforced_with_feedback(series, bad, expected):
    llm = ScriptedLLM(bad, _plan())
    Planner(llm, series).plan(5, [])
    assert expected in llm.users[1]


def test_unknown_cast_is_rejected_and_known_cast_is_normalised(series):
    bad = _plan()
    bad["shots"][0]["cast"] = ["Batman"]
    good = _plan()
    good["shots"][0]["cast"] = ["kai"]
    llm = ScriptedLLM(bad, good)
    plan = Planner(llm, series).plan(5, [])
    assert "unknown" in llm.users[1]
    assert plan.shots[0].cast == ["Kai"]


def test_calendar_category_overrides_model(series):
    plan = Planner(ScriptedLLM(_plan(category="Other", emoji="❓")), series).plan(1, [])
    assert plan.category == series.episodes[0].category and plan.emoji == series.episodes[0].emoji


def test_gives_up_after_max_attempts_and_sends_past_titles(series):
    llm = ScriptedLLM(*[{} for _ in range(Planner(None, series).max_attempts)])
    with pytest.raises(RuntimeError, match="planner failed"):
        Planner(llm, series).plan(50, ["LAST TRAIN (Metro)"])
    assert "LAST TRAIN (Metro)" in llm.users[0] and "No category planned" in llm.users[0]


def test_system_prompt_has_rules_and_cast_but_not_style(series):
    sp = Planner(ScriptedLLM(), series).system_prompt()
    assert "hook -> escalate -> clash -> climax -> twist" in sp
    assert "Kai:" in sp and "Never the same size twice in a row" in sp
    assert series.style.bible.strip()[:30] not in sp  # style is appended by code, not the LLM
