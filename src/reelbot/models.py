"""Data shapes passed between pipeline stages."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Beat = Literal["hook", "escalate", "clash", "climax", "twist"]
ShotSize = Literal["EWS", "WS", "MS", "MCU", "CU", "ECU"]

BEATS: tuple[Beat, ...] = ("hook", "escalate", "clash", "climax", "twist")


class Location(BaseModel):
    name: str = Field(min_length=2, description="short name, e.g. 'rainy metro platform'")
    plate_prompt: str = Field(min_length=20, description="the empty environment, no people, no text")


class ShotPlan(BaseModel):
    beat: Beat
    size: ShotSize
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

    @field_validator("title")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class JobStatus(StrEnum):
    PLANNED = "planned"
    GENERATING = "generating"
    AWAITING_REVIEW = "awaiting_review"
    APPROVED = "approved"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    REJECTED = "rejected"
    FAILED = "failed"
