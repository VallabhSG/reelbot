"""LLM planner: turns the series bible + episode number into a validated EpisodePlan."""

from __future__ import annotations

import json
import logging

from pydantic import ValidationError

from .models import EpisodePlan
from .providers import JSONChat
from .series import Series

log = logging.getLogger(__name__)

SYSTEM = """You are the showrunner of a short-form AI animation series on Instagram Reels.
Each episode is a {seconds}-second silent micro-film told in exactly {n} shots, followed by music.
The story must land visually with no dialogue: setup, escalation, a twist or payoff in the last shot.

SERIES: {name}
PREMISE: {premise}
VISUAL STYLE (applies to every shot): {style}
RECURRING CHARACTERS:
{characters}

Rules:
- title: 1-2 punchy words, ALL CAPS, max 14 characters, used on the title card.
- hook: the caption opener. 1-2 short lines, curiosity gap, no hashtags, no emojis except an optional one at start.
- keyframe_prompt: a single still frame. Describe subject, pose, setting, lighting, lens/composition, 9:16
  vertical framing. Name recurring characters by their description, not just their name. Do NOT restate the
  style; it is appended automatically.
- motion_prompt: only what moves: camera move + subject action within ~{shot_s}s. One clear action per shot.
- music_prompt: genre, mood, tempo, instruments, "instrumental". Never name real artists, songs or films.
- Never use copyrighted characters, brands, logos, real people or real sports teams.

Respond with JSON only:
{{"title": str, "category": str, "emoji": str, "logline": str, "hook": str,
  "shots": [{{"keyframe_prompt": str, "motion_prompt": str}}, ...exactly {n}],
  "music_prompt": str}}"""


class Planner:
    def __init__(self, llm: JSONChat, series: Series, max_attempts: int = 3):
        self.llm = llm
        self.series = series
        self.max_attempts = max_attempts

    def system_prompt(self) -> str:
        s = self.series
        chars = (
            "\n".join(f"- {c.name}: {c.description}" for c in s.characters) or "- (none; invent per episode)"
        )
        return SYSTEM.format(
            seconds=s.shots.count * s.shots.duration_s,
            n=s.shots.count,
            shot_s=s.shots.duration_s,
            name=s.series.name,
            premise=s.series.premise,
            style=s.style.bible,
            characters=chars,
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
            lines.append(f"Your previous answer was invalid: {feedback}. Fix it.")
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
            n = self.series.shots.count
            if len(plan.shots) < n:
                feedback = f"needed exactly {n} shots, got {len(plan.shots)}"
                continue
            plan.shots = plan.shots[:n]
            seed = (
                self.series.episodes[episode_no - 1] if episode_no - 1 < len(self.series.episodes) else None
            )
            if seed:  # the calendar wins over the model for category/emoji
                plan.category, plan.emoji = seed.category, seed.emoji or plan.emoji
            return plan
        raise RuntimeError(f"planner failed after {self.max_attempts} attempts: {feedback}")
