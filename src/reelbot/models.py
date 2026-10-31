"""Data shapes passed between pipeline stages."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Beat = Literal["hook", "escalate", "clash", "climax", "twist"]
ShotSize = Literal["EWS", "WS", "MS", "MCU", "CU", "ECU"]

BEATS: tuple[Beat, ...] = ("hook", "escalate", "clash", "climax", "twist")

# The planner LLM sometimes spells these out ("extreme close-up", "escalation"): accept the near
# misses instead of throwing the whole plan away (a day with 4 failed attempts makes no episode).
SIZE_ALIASES = {
    "EXTREMECLOSEUP": "ECU",
    "XCU": "ECU",
    "BCU": "ECU",
    "EXTREMECU": "ECU",
    "MACRO": "ECU",
    "CLOSEUP": "CU",
    "CLOSE": "CU",
    "MEDIUMCLOSEUP": "MCU",
    "MEDIUMCU": "MCU",
    "MEDIUMSHOT": "MS",
    "MEDIUM": "MS",
    "MID": "MS",
    "MIDSHOT": "MS",
    "MEDIUMWIDE": "MS",
    "MWS": "MS",
    "WIDESHOT": "WS",
    "WIDE": "WS",
    "LS": "WS",
    "LONGSHOT": "WS",
    "FULLSHOT": "WS",
    "FS": "WS",
    "EXTREMEWIDESHOT": "EWS",
    "EXTREMEWIDE": "EWS",
    "ELS": "EWS",
    "EXTREMELONGSHOT": "EWS",
    "ESTABLISHING": "EWS",
    "ESTABLISHINGSHOT": "EWS",
    "XWS": "EWS",
}
BEAT_ALIASES = {
    "setup": "hook",
    "intro": "hook",
    "opening": "hook",
    "open": "hook",
    "stakes": "hook",
    "escalation": "escalate",
    "build": "escalate",
    "buildup": "escalate",
    "rise": "escalate",
    "fight": "clash",
    "action": "clash",
    "conflict": "clash",
    "battle": "clash",
    "peak": "climax",
    "winningmove": "climax",
    "resolution": "twist",
    "payoff": "twist",
    "punchline": "twist",
    "ending": "twist",
    "end": "twist",
    "outro": "twist",
}


def _key(v: str) -> str:
    return "".join(ch for ch in v if ch.isalnum())


class Location(BaseModel):
    name: str = Field(min_length=2, description="short name, e.g. 'rainy metro platform'")
    plate_prompt: str = Field(min_length=20, description="the empty environment, no people, no text")


class ShotPlan(BaseModel):
    beat: Beat
    size: ShotSize

    @field_validator("size", mode="before")
    @classmethod
    def _size_alias(cls, v: object) -> object:
        if isinstance(v, str):
            k = _key(v).upper()
            return SIZE_ALIASES.get(k, k)
        return v

    @field_validator("beat", mode="before")
    @classmethod
    def _beat_alias(cls, v: object) -> object:
        if isinstance(v, str):
            k = _key(v).lower()
            return BEAT_ALIASES.get(k, k)
        return v

    angle: str = Field(min_length=3, description="eye level, low angle, high angle, overhead, POV, dutch")
    camera: str = Field(min_length=3, description="one physical camera move")
    cast: list[str] = Field(default_factory=list, max_length=3)
    action: str = Field(min_length=8, description="one action with a visible end state")
    light: str = Field(min_length=3, description="light direction + colour temperature")
    # v3 (one-take): timing + continuity. Defaults keep v2 plans valid.
    seconds: int = Field(3, ge=1, le=8, description="length of this shot inside the take")
    ends: str = Field("", description="the exact visible state at the cut; the next shot starts here")
    sound: str = Field("", description="the one diegetic sound of this shot, no speech")
    line: str = Field("", description="optional short spoken line (dialogue bookends only)")
    speaker: str = Field("", description="who says the line: a speaking character in this shot")
    framing: str = Field("", description="where everyone and every key prop sits in the frame")
    performance: str = Field("", description="face and body acting plus secondary motion")
    fx: str = Field("", description="the anime effect drawn in the picture, if any")
    # written by the director's pass after planning (director.py): the whole shot in full
    detail: str = Field("", description="the director's full description of the shot")


class EpisodePlan(BaseModel):
    title: str = Field(min_length=1, max_length=24, description="1-2 word title card, e.g. DIVEBOMB")
    category: str
    emoji: str = ""
    logline: str
    hook: str = Field(description="1-2 line micro-story caption hook")
    location: Location
    shots: list[ShotPlan] = Field(min_length=1)
    music_prompt: str
    # v3 (one-take)
    winning_move: str = Field("", description="the one decisive action the episode is built backward from")
    motif: str = Field("", description="one small object/colour that recurs and pays off")
    direction: str = Field("", description="screen direction kept in every shot, e.g. left to right")
    opening_frame: str = Field("", description="the first frame: who is where, pose, framing")
    layout: str = Field("", description="the fixed geography: where each key prop is, distances")
    # written by the director's pass (director.py): every element of the set, and where everyone
    # and every key prop stands in the opening frame
    world: str = Field("", description="every element of the set, fixed for the episode")
    blocking: str = Field("", description="first-frame positions, screen percentages and distances")

    @field_validator("title")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()

    @model_validator(mode="before")
    @classmethod
    def _find_shots(cls, data: object) -> object:
        return recover_shots(data)

    @model_validator(mode="before")
    @classmethod
    def _music_prompt(cls, data: object) -> object:
        """A forgotten music prompt shouldn't cost a whole plan: take the model's other spelling,
        or a neutral cue that fits every episode of the format."""
        if isinstance(data, dict) and not str(data.get("music_prompt") or "").strip():
            alt = data.get("music") or data.get("music_style") or data.get("score")
            data = {
                **data,
                "music_prompt": alt
                if isinstance(alt, str) and alt.strip()
                else "playful anime action score that builds to one big hit, then a short comic sting",
            }
        return data


SHOT_KEYS = ("storyboard", "shot_list", "shotlist", "shots_list", "scenes", "shot_plan")


def recover_shots(data: object) -> object:
    """A plan whose shots sit under another name, or a plan wrapped in one key ({"episode":
    {...}}), or shots as a numbered object, is still the plan."""
    if not isinstance(data, dict) or isinstance(data.get("shots"), list):
        return data
    wrapped = [v for v in data.values() if isinstance(v, dict) and "shots" in v]
    if len(wrapped) == 1 and "title" not in data:
        return recover_shots({**{k: v for k, v in data.items() if not isinstance(v, dict)}, **wrapped[0]})
    for key in SHOT_KEYS:
        alt = data.get(key)
        if isinstance(alt, dict) and isinstance(alt.get("shots"), list):
            alt = alt["shots"]
        if isinstance(alt, list) and alt and all(isinstance(x, dict) for x in alt):
            return {**data, "shots": alt}
    shots = data.get("shots")
    if isinstance(shots, dict) and shots and all(isinstance(x, dict) for x in shots.values()):
        return {**data, "shots": list(shots.values())}  # {"1": {...}, "2": {...}}
    return data


class JobStatus(StrEnum):
    PLANNED = "planned"
    GENERATING = "generating"
    AWAITING_REVIEW = "awaiting_review"
    APPROVED = "approved"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    REJECTED = "rejected"
    FAILED = "failed"
