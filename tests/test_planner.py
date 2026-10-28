import pytest

from reelbot.planner import Planner


def _plan(n_shots, **over):
    base = {
        "title": "divebomb",
        "category": "Kart",
        "emoji": "🏎️",
        "logline": "Two karts, one corner.",
        "hook": "Same move, different price.",
        "shots": [
            {"keyframe_prompt": f"wide shot of kart {i} at apex", "motion_prompt": "pan left"}
            for i in range(n_shots)
        ],
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


def test_retries_with_validation_feedback(series):
    llm = ScriptedLLM({"title": "X"}, _plan(3))
    plan = Planner(llm, series).plan(5, [])
    assert plan.title == "DIVEBOMB"  # upper-cased
    assert "previous answer was invalid" in llm.users[1]


def test_too_few_shots_is_retried_and_extra_shots_trimmed(series):
    llm = ScriptedLLM(_plan(2), _plan(5))
    plan = Planner(llm, series).plan(20, [])
    assert len(plan.shots) == series.shots.count
    assert "needed exactly 3 shots" in llm.users[1]


def test_calendar_category_overrides_model(series):
    llm = ScriptedLLM(_plan(3, category="Something else", emoji="❓"))
    plan = Planner(llm, series).plan(1, [])
    assert plan.category == series.episodes[0].category
    assert plan.emoji == series.episodes[0].emoji
    assert "Planned category" in llm.users[0]


def test_past_titles_are_sent_and_failure_is_raised(series):
    llm = ScriptedLLM({}, {}, {})
    with pytest.raises(RuntimeError, match="planner failed"):
        Planner(llm, series).plan(50, ["LAST TRAIN (Metro)"])
    assert "LAST TRAIN (Metro)" in llm.users[0]
    assert "No category planned" in llm.users[0]


def test_system_prompt_carries_style_and_rules(series):
    sp = Planner(ScriptedLLM(), series).system_prompt()
    assert series.style.bible.strip()[:30] in sp
    assert "Never name real artists" in sp
