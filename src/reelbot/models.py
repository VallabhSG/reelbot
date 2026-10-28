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


class EpisodePlan(BaseModel):
    title: str = Field(min_length=1, max_length=24, description="1-2 word title card, e.g. DIVEBOMB")
    category: str
    emoji: str = ""
    logline: str
    hook: str = Field(description="1-2 line micro-story caption hook")
    location: Location
    shots: list[ShotPlan] = Field(min_length=1)
    music_prompt: str

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
