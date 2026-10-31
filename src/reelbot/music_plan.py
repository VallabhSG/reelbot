"""The score, written to the story: one music section per story beat, each as long as its shots.

A single text prompt gave episode 9 a flat loop: the same beat at the same level from 1 s to 14 s,
nothing building, nothing landing on the climax. ElevenLabs' newer music models take a composition
plan instead: a list of sections ("chunks"), each with its own length and styles. So the music
follows the reel: a tense intro while the problem appears, a build as she commits, the battle
theme for the clash, one huge hit on the climax, then a fanfare (triumph) or a dead stop and a
comic sting (deadpan). Sections are at least 3 s long (the API's minimum); shorter beats merge
into their neighbour."""

from __future__ import annotations

from typing import Any

from .models import EpisodePlan
from .series import Series

MIN_MS = 3000  # the API's shortest section

# (section name, what happens, styles) per story beat
SECTIONS: dict[str, tuple[str, str, list[str]]] = {
    "hook": (
        "Intro",
        "the problem appears: quiet, tense, waiting",
        ["sparse and tense", "ticking hi-hat", "low synth pulse", "suspense", "soft dynamics"],
    ),
    "escalate": (
        "Build",
        "she powers up: rising, getting louder every bar",
        ["rising tension", "snare roll building", "strings climbing", "crescendo", "anticipation"],
    ),
    "clash": (
        "Battle",
        "the fight: full energy, the main theme",
        [
            "epic anime battle theme",
            "driving drums",
            "fast string ostinato",
            "heroic brass stabs",
            "electric guitar riff",
            "full energy",
        ],
    ),
    "climax": (
        "Climax",
        "the decisive move: one massive hit, then it rings out",
        ["one massive orchestral hit", "huge cymbal crash", "slow-motion swell", "loudest moment"],
    ),
}
TRIUMPH = (
    "Victory",
    "she wins: a short proud fanfare that resolves",
    ["triumphant brass fanfare", "resolves on a bright major chord", "short and proud", "final hit"],
)
DEADPAN = (
    "Deadpan",
    "the anticlimax: the band stops dead, then one small comic sting",
    ["music stops dead", "near silence", "a tiny comic sting", "single plucked note", "deadpan"],
)
NEGATIVE = ["vocals", "singing", "lyrics", "spoken words", "humming", "choir words"]


def _global_styles(prompt: str, bpm: int) -> list[str]:
    """The planner's music prompt as style phrases, plus the tempo the cuts are timed to."""
    styles = [p.strip() for p in prompt.replace(";", ",").split(",") if p.strip()]
    styles = [s for s in styles if "bpm" not in s.lower()][:10]
    return [*styles, "instrumental", f"{bpm} BPM"] if bpm else [*styles, "instrumental"]


def music_chunks(series: Series, plan: EpisodePlan, triumph: bool) -> list[dict[str, Any]]:
    """The composition plan's chunks, timed to the shots; they add up to the reel's length."""
    sections: list[dict[str, Any]] = []
    for s in plan.shots:
        name, what, styles = (TRIUMPH if triumph else DEADPAN) if s.beat == "twist" else SECTIONS[s.beat]
        if sections and sections[-1]["name"] == name:
            sections[-1]["ms"] += s.seconds * 1000
        else:
            sections.append({"name": name, "what": [what], "styles": list(styles), "ms": s.seconds * 1000})
    # merge anything under the API's minimum into the section after it (or before, at the end)
    i = 0
    while i < len(sections) and len(sections) > 1:
        if sections[i]["ms"] >= MIN_MS:
            i += 1
            continue
        j = i + 1 if i + 1 < len(sections) else i - 1
        a, b = sorted((i, j))
        merged = {
            "name": f"{sections[a]['name']} into {sections[b]['name']}",
            "what": sections[a]["what"] + sections[b]["what"],
            "styles": list(dict.fromkeys(sections[a]["styles"] + sections[b]["styles"])),
            "ms": sections[a]["ms"] + sections[b]["ms"],
        }
        sections[a : b + 1] = [merged]
        i = a
    base = _global_styles(plan.music_prompt, series.take.music_bpm)
    return [
        {
            "text": f"[{sec['name']}]\n{{instrumental: {'; then '.join(sec['what'])}}}",
            "duration_ms": sec["ms"],
            "positive_styles": list(dict.fromkeys(base + sec["styles"]))[:50],
            "negative_styles": NEGATIVE,
        }
        for sec in sections
    ]
