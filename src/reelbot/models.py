"""Data shapes passed between pipeline stages."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class Shot(BaseModel):
    keyframe_prompt: str = Field(min_length=10, description="what the still frame shows")
    motion_prompt: str = Field(min_length=5, description="camera + action for image-to-video")


class EpisodePlan(BaseModel):
    title: str = Field(min_length=1, max_length=24, description="1-2 word title card, e.g. DIVEBOMB")
    category: str
    emoji: str = ""
    logline: str
    hook: str = Field(description="1-2 line micro-story caption hook")
    shots: list[Shot] = Field(min_length=1)
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
