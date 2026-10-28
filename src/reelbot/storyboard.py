"""Storyboard rules + per-shot prompt composition.

Recipe (from the reference reels and the Seedance/Kling prompting guides):
  * fixed beat order: hook -> escalate -> clash -> climax -> twist
  * shot-size variety: never the same size twice in a row, at least one close shot (CU/ECU)
    and one wide shot (WS/EWS) per reel
  * ONE location per episode, fed to every shot as a reference image, so scenes don't jump
  * every recurring character fed as a reference image in every shot they appear in
  * prompt formula: size + angle, camera move as a physical behaviour, ONE subject action with an
    end state, setting + light, style lock, negatives last
"""

from __future__ import annotations

from .models import BEATS, EpisodePlan, ShotPlan
from .series import Series

SIZE_TEXT = {
    "EWS": "Extreme wide shot",
    "WS": "Wide shot",
    "MS": "Medium shot",
    "MCU": "Medium close-up",
    "CU": "Close-up",
    "ECU": "Extreme close-up",
}
CLOSE = {"CU", "ECU"}
WIDE = {"WS", "EWS"}


def canonical_cast(series: Series, names: list[str]) -> tuple[list[str], list[str]]:
    """Map names case-insensitively to series characters. Returns (known, unknown)."""
    lookup = {c.name.lower(): c.name for c in series.characters}
    known, unknown = [], []
    for n in names:
        hit = lookup.get(n.strip().lower())
        if hit and hit not in known:
            known.append(hit)
        elif not hit:
            unknown.append(n)
    return known, unknown


def storyboard_problems(series: Series, plan: EpisodePlan) -> list[str]:
    """Rule violations, phrased as instructions the LLM can act on."""
    out: list[str] = []
    n = series.shots.count
    if len(plan.shots) != n:
        out.append(f"there must be exactly {n} shots, got {len(plan.shots)}")
    beats = tuple(s.beat for s in plan.shots)
    if n == len(BEATS) and beats != BEATS:
        out.append(f"beats must be exactly in this order: {', '.join(BEATS)} (got {', '.join(beats)})")
    sizes = [s.size for s in plan.shots]
    for i in range(1, len(sizes)):
        if sizes[i] == sizes[i - 1]:
            out.append(f"shots {i} and {i + 1} are both {sizes[i]}; consecutive shots need different sizes")
    if not CLOSE & set(sizes):
        out.append("include at least one CU or ECU shot")
    if not WIDE & set(sizes):
        out.append("include at least one WS or EWS shot")
    for i, s in enumerate(plan.shots, 1):
        _, unknown = canonical_cast(series, s.cast)
        if unknown:
            out.append(
                f"shot {i} cast {unknown} unknown; use only {[c.name for c in series.characters]} or []"
            )
    if series.characters and not any(s.cast for s in plan.shots):
        out.append("the recurring characters must appear in the episode")
    return out


def normalise(series: Series, plan: EpisodePlan) -> EpisodePlan:
    for s in plan.shots:
        s.cast, _ = canonical_cast(series, s.cast)
    return plan


def compose_shot_prompt(
    series: Series, plan: EpisodePlan, shot: ShotPlan, token: str
) -> tuple[str, list[str]]:
    """Returns (prompt, ordered reference roles). Roles are character names plus 'location' last;
    the caller turns them into image URLs in the same order, so @image_N lines up."""
    roles = [*shot.cast, "location"]
    tag = {role: token.format(n=i + 1) for i, role in enumerate(roles)}
    chars = {c.name: c for c in series.characters}

    subjects = ", ".join(
        f"{name} ({tag[name]}, {chars[name].short or chars[name].description})" for name in shot.cast
    )
    parts = [
        f"{SIZE_TEXT[shot.size]}, {shot.angle}, {shot.camera}.",
        f"{subjects}: {shot.action}." if subjects else f"{shot.action}.",
        f"Setting: the {plan.location.name} shown in {tag['location']}, {shot.light}.",
        series.style.bible.strip().rstrip(".") + ".",
    ]
    if shot.cast:
        parts.append(
            "Keep every character's face, hair, outfit and colours exactly as in their reference image."
        )
    if series.style.negative:
        parts.append(series.style.negative.strip())
    prompt = " ".join(p for p in parts if p)
    return prompt, roles
