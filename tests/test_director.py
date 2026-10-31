"""The director's pass: set, blocking and every shot written out in full, never changing clothes."""

import pytest

from reelbot.director import SET_ROLE, SHOT_ROLE, Director, clothes_problem, shot_words
from reelbot.storyboard import compose_take_prompt, normalise

from .test_take import _directed_plan, _plan


def _words(n: int, word: str = "tile") -> str:
    return " ".join([word] * n)


class FakeLLM:
    """Answers the director's calls from queues; records every user message."""

    def __init__(self, shots: list[str] | None = None, world: int = 180, blocking: int = 70):
        self.shots = list(shots or [])
        self.world, self.blocking = world, blocking
        self.calls: list[tuple[str, str]] = []

    def json_chat(self, system, user, temperature=0.9):
        self.calls.append((system, user))
        if SET_ROLE in system:
            return {"set": _words(self.world), "blocking": _words(self.blocking, "x30%")}
        assert SHOT_ROLE in system
        if self.shots:
            return {"detail": self.shots.pop(0)}
        lo, _ = shot_words(4)
        return {"detail": _words(lo, "lens")}


@pytest.mark.parametrize(
    "text",
    [
        "She pulls her hoodie off over her head and throws it aside.",
        "Ren takes off his glasses.",
        "Juno unzips her hoodie in one sharp tug.",
        "Mochi's scarf flies off in the gust.",
        "She slips out of her sneakers.",
        "He changes into a tracksuit.",
    ],
)
def test_clothes_changes_are_caught(text):
    assert clothes_problem(text)


@pytest.mark.parametrize(
    "text",
    [
        "She pulls the plug out of the socket.",
        "Lamp light bounces off her hoodie.",
        "Her hoodie strings swing as she lands.",
        "The cable slides off the sofa arm.",
        "Juno takes the phone off the charger.",
    ],
)
def test_ordinary_action_is_not_a_clothes_change(text):
    assert not clothes_problem(text)


def test_direct_writes_the_set_the_blocking_and_every_shot(take_series):
    plan = normalise(take_series, _plan())
    llm = FakeLLM()
    out = Director(llm, take_series).direct(plan)
    assert len(out.world.split()) == 180 and out.blocking.startswith("x30%")
    assert all(s.detail.startswith("lens") for s in out.shots)
    assert [s.action for s in out.shots] == [s.action for s in plan.shots]  # the story is untouched
    shot_calls = [u for sys, u in llm.calls if SHOT_ROLE in sys]
    assert len(shot_calls) == len(plan.shots)
    assert "OPENING FRAME BLOCKING" in shot_calls[0] and "PREVIOUS SHOT ends on" in shot_calls[1]
    assert "NEXT SHOT starts with" in shot_calls[0] and "NEXT SHOT" not in shot_calls[-1]
    assert all("SET: tile" in u for u in shot_calls)


def test_a_shot_that_changes_clothes_is_asked_again_then_dropped(take_series):
    plan = normalise(take_series, _plan())
    lo, _ = shot_words(plan.shots[0].seconds)
    bad = "Kai pulls his jacket off over his head. " + _words(lo)
    good = _words(lo, "lens")
    llm = FakeLLM(shots=[bad, good, bad, bad])
    out = Director(llm, take_series).direct(plan)
    assert out.shots[0].detail == good  # fixed on the second try
    assert out.shots[1].detail == ""  # still wrong: falls back to the storyboard fields
    rejected = [u for sys, u in llm.calls if SHOT_ROLE in sys and "WAS REJECTED" in u]
    assert rejected and "changes someone's clothes" in rejected[0]


def test_a_too_short_shot_is_asked_again(take_series):
    plan = normalise(take_series, _plan())
    lo, hi = shot_words(plan.shots[0].seconds)
    llm = FakeLLM(shots=[_words(10), _words(lo, "lens")])
    out = Director(llm, take_series).direct(plan)
    assert out.shots[0].detail.startswith("lens")
    assert any(f"write {lo}-{hi}" in u for _, u in llm.calls)


def test_a_failing_shot_costs_only_that_shot(take_series):
    plan = normalise(take_series, _plan())

    class Flaky(FakeLLM):
        def json_chat(self, system, user, temperature=0.9):
            if SHOT_ROLE in system and "WRITE SHOT 2 " in user:
                raise RuntimeError("model hiccup")
            return super().json_chat(system, user, temperature)

    out = Director(Flaky(), take_series).direct(plan)
    assert out.shots[1].detail == "" and out.shots[0].detail and out.shots[2].detail


def test_the_take_prompt_uses_the_directors_shots_and_set(take_series):
    take_series.characters[0].look = "spiky black hair, red track jacket, grey joggers"
    plan = Director(FakeLLM(), take_series).direct(normalise(take_series, _directed_plan()))
    p = compose_take_prompt(take_series, plan, "", start_frame=True).prompt
    assert "[SET, every element fixed in every shot] tile tile" in p
    assert "[FIRST FRAME AND BLOCKING] x30%" in p
    shot1 = next(ln for ln in p.splitlines() if ln.startswith("Shot 1 (2 s, HOOK):"))
    assert "In frame: Kai (spiky black hair, red track jacket, grey joggers, unchanged). lens lens" in shot1
    assert "Framing:" not in shot1 and "Sound:" not in shot1  # the director's text covers them
    assert p.index("[PHYSICS]") < p.index("[LOCKS]") < p.index("[NEGATIVE]")


def test_over_the_limit_the_directors_shots_give_way_to_the_storyboard(take_series):
    plan = Director(FakeLLM(), take_series).direct(normalise(take_series, _directed_plan()))
    full = compose_take_prompt(take_series, plan, "", start_frame=True).prompt
    tight = compose_take_prompt(take_series, plan, "", start_frame=True, max_chars=len(full) // 2).prompt
    assert len(tight) <= len(full) // 2 and "Framing: Kai lower-left third" in tight
