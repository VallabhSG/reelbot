"""LLM planner: series bible + episode number -> a validated, rule-checked storyboard.

The LLM only fills structured fields (beat, shot size, angle, camera, cast, one action, light);
storyboard.compose_shot_prompt turns them into model prompts with a fixed formula, so prompt
quality doesn't depend on the LLM's prose.
"""

from __future__ import annotations

import json
import logging

from pydantic import ValidationError

from .models import BEATS, EpisodePlan
from .providers import JSONChat
from .series import Series
from .storyboard import normalise, storyboard_problems

log = logging.getLogger(__name__)

SYSTEM = """You are the director of a wordless anime micro-film series for Instagram Reels.
Each episode is {seconds} seconds: exactly {n} shots of {shot_s} seconds, then music. No dialogue,
no on-screen text: the story must read from the pictures alone.

SERIES: {name}
PREMISE: {premise}
RECURRING CAST (reference images are supplied automatically; never describe their looks):
{characters}

STORYBOARD RULES
- Beats, in this exact order: {beats}.
  hook = an instantly readable situation with stakes in the first second;
  escalate = the character commits, tension rises;
  clash = the big dynamic action, energy, motion;
  climax = the decisive instant, often a close or slow-motion moment;
  twist = a deadpan, funny anticlimax that recontextualises everything.
- Shot sizes: EWS, WS, MS, MCU, CU, ECU. Never the same size twice in a row. At least one CU or ECU
  and at least one WS or EWS. Vary angles: eye level, low angle, high angle, overhead, POV, dutch angle.
- camera: exactly ONE physical move per shot, e.g. "static locked-off", "slow push-in", "fast push-in",
  "pull-back reveal", "lateral tracking", "low tracking", "orbit", "crane up", "handheld follow",
  "whip pan", "slow-motion speed ramp" (only on climax).
- action: ONE action with a concrete verb and a visible end state, max 25 words. Refer to cast by name
  only. No dialogue, no thoughts, no text, no signs.
- cast: names from the cast list in that shot (max 2), [] for an empty shot.
- ONE location for the whole episode. location.plate_prompt describes that place EMPTY (no people, no
  animals), with time of day, weather and light, and blank, unreadable signage.
- light: direction + colour temperature, consistent across shots (same time of day).
- hook (the caption, not a shot): 1-2 short lines, curiosity gap, no hashtags.
- music_prompt: genre, mood, tempo, instruments, "instrumental". Never real artists or songs.
- Never use copyrighted characters, brands, logos, real people or real teams.

Respond with JSON only:
{{"title": str (1-2 words, max 14 chars), "category": str, "emoji": str, "logline": str, "hook": str,
  "location": {{"name": str, "plate_prompt": str}},
  "shots": [{{"beat": str, "size": str, "angle": str, "camera": str, "cast": [str], "action": str,
              "light": str}}, ... exactly {n}],
  "music_prompt": str}}"""


class Planner:
    def __init__(self, llm: JSONChat, series: Series, max_attempts: int = 4):
        self.llm = llm
        self.series = series
        self.max_attempts = max_attempts

    def system_prompt(self) -> str:
        s = self.series
        chars = "\n".join(f"- {c.name}: {c.description}" for c in s.characters) or "- (none)"
        return SYSTEM.format(
            seconds=s.shots.count * s.shots.duration_s,
            n=s.shots.count,
            shot_s=s.shots.duration_s,
            name=s.series.name,
            premise=s.series.premise,
            characters=chars,
            beats=" -> ".join(BEATS[: s.shots.count]) if s.shots.count == len(BEATS) else "free",
        )

    def user_prompt(self, episode_no: int, past_titles: list[str], feedback: str = "") -> str:
        s = self.series
        seed = s.episodes[episode_no - 1] if episode_no - 1 < len(s.episodes) else None
        lines = [
            f"EPISODE {episode_no} of {s.series.total_episodes}.",
            f"SHOT_COUNT_JSON={json.dumps(s.shots.count)}",
        ]
        if seed:
            lines.append(f"Planned category: {seed.emoji} {seed.category}".strip())
            if seed.idea:
                lines.append(f"Idea to build on: {seed.idea}")
        else:
            lines.append("No category planned: pick a fresh one that fits the premise.")
        if past_titles:
            lines.append("Already used (do not repeat titles or premises): " + "; ".join(past_titles))
        if feedback:
            lines.append(f"Your previous answer broke these rules: {feedback}. Fix them and answer again.")
        return "\n".join(lines)

    def plan(self, episode_no: int, past_titles: list[str]) -> EpisodePlan:
        feedback = ""
        for attempt in range(1, self.max_attempts + 1):
            raw = self.llm.json_chat(
                self.system_prompt(), self.user_prompt(episode_no, past_titles, feedback)
            )
            try:
                plan = EpisodePlan.model_validate(raw)
            except ValidationError as e:
                feedback = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
                log.warning("plan attempt %d invalid: %s", attempt, feedback)
                continue
            problems = storyboard_problems(self.series, plan)
            if problems:
                feedback = "; ".join(problems)
                log.warning("plan attempt %d breaks storyboard rules: %s", attempt, feedback)
                continue
            plan = normalise(self.series, plan)
            seed = (
                self.series.episodes[episode_no - 1] if episode_no - 1 < len(self.series.episodes) else None
            )
            if seed:  # the calendar wins over the model for category/emoji
                plan.category, plan.emoji = seed.category, seed.emoji or plan.emoji
            return plan
        raise RuntimeError(f"planner failed after {self.max_attempts} attempts: {feedback}")
