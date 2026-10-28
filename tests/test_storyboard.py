from reelbot.models import EpisodePlan
from reelbot.storyboard import compose_shot_prompt


def _plan(series):
    return EpisodePlan.model_validate(
        {
            "title": "LAST TRAIN",
            "category": "Metro",
            "logline": "l",
            "hook": "h",
            "location": {"name": "night metro platform", "plate_prompt": "an empty metro platform at night"},
            "shots": [
                {
                    "beat": "hook",
                    "size": "WS",
                    "angle": "eye level",
                    "camera": "static locked-off",
                    "cast": ["Kai"],
                    "action": "Kai sprints for the closing doors",
                    "light": "cold key, 5600K",
                },
                {
                    "beat": "escalate",
                    "size": "ECU",
                    "angle": "low angle",
                    "camera": "fast push-in",
                    "cast": [],
                    "action": "the door edges slam shut",
                    "light": "cold key, 5600K",
                },
            ],
            "music_prompt": "m",
        }
    )


def test_prompt_follows_formula_and_numbers_refs_in_order(series):
    plan = _plan(series)
    prompt, roles = compose_shot_prompt(series, plan, plan.shots[0], "@image_{n}")
    assert roles == ["Kai", "location"]
    assert prompt.startswith("Wide shot, eye level, static locked-off.")
    assert "Kai (@image_1, lanky courier in round glasses and an orange jacket): Kai sprints" in prompt
    assert "night metro platform shown in @image_2" in prompt
    assert "exactly as in their reference image" in prompt
    assert prompt.rstrip().endswith(series.style.negative.strip())  # negatives last


def test_empty_shot_only_references_the_location(series):
    plan = _plan(series)
    prompt, roles = compose_shot_prompt(series, plan, plan.shots[1], "@Image{n}")
    assert roles == ["location"]
    assert prompt.startswith("Extreme close-up, low angle, fast push-in.")
    assert "@Image1" in prompt and "reference image." not in prompt
