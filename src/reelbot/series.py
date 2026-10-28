"""Series definition: the creative + model config for one account, loaded from YAML."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class Character(BaseModel):
    name: str
    description: str  # full design, used for the one-time character sheet
    short: str = ""  # a few words repeated in every shot prompt next to the reference tag
    reference_images: list[Path] = Field(default_factory=list)  # optional hand-made refs (else: cast)


class CastSpec(BaseModel):
    """One-time character sheets: generated, approved in Telegram, then reused in every shot."""

    candidates: int = Field(3, ge=1, le=6)
    aspect_ratio: str = "16:9"
    sheet_template: str = (
        "Character reference sheet for an animated film: {description}. Full-body front view, "
        "three-quarter view, side profile, and three facial expressions (neutral, determined, surprised), "
        "same design in every view, plain light-grey background, even studio lighting. {style} "
        "No text, no labels, no watermark."
    )


class EpisodeSeed(BaseModel):
    """A planned episode. Anything left out is filled in by the LLM planner."""

    category: str
    emoji: str = ""
    idea: str = ""


class SeriesMeta(BaseModel):
    name: str
    handle: str
    premise: str
    total_episodes: int = 100
    header_template: str = "{series}\nDay {n} / {total}\n{emoji} {category}"


class Style(BaseModel):
    bible: str
    negative: str = ""
    motion_suffix: str = ""


class ShotSpec(BaseModel):
    count: int = Field(3, ge=1, le=8)
    duration_s: int = Field(5, ge=2, le=30)


class ModelSpec(BaseModel):
    """One Higgsfield endpoint. `input` is a template; see templating.render."""

    endpoint: str
    input: dict[str, Any]
    output_path: str
    cost_per_call: float = 0.0
    cost_per_second: float = 0.0
    ref_token: str = "@image_{n}"  # how this model addresses reference images inside the prompt

    def estimate(self, seconds: float = 0.0) -> float:
        return round(self.cost_per_call + self.cost_per_second * seconds, 4)


class Models(BaseModel):
    image: ModelSpec
    video: ModelSpec


class MusicSpec(BaseModel):
    provider: Literal["elevenlabs", "library", "none"] = "elevenlabs"
    library_dir: Path = Path("assets/music")
    model_id: str | None = None
    cost_per_track: float = 0.0
    volume_db: float = -2.0


class TitleCard(BaseModel):
    font: Path | None = None
    subtitle: str = "A FILM BY {handle}"
    show_from: float = 0.3
    show_until: float = 2.8
    color: str = "#FFFFFF"
    stroke_color: str = "#111111"
    stroke_width: int = 6
    y_ratio: float = Field(0.62, ge=0.1, le=0.9, description="vertical position of the title, 0=top")
    in_video: bool = False  # burn the title into the first seconds (off: keep the hook shot clean)
    cover: bool = True  # poster cover (best frame + title) uploaded as the Reels cover image
    cover_offset_ms: int = 1200  # fallback cover frame if no poster cover is used


class CaptionSpec(BaseModel):
    template: str = "{header}\n\n{hook}\n\n{credits}\n\n{hashtags}"
    credits: str = ""
    hashtags: list[str] = Field(default_factory=list)


class Budget(BaseModel):
    monthly_usd: float = 40.0
    per_reel_usd: float = 3.0


class Schedule(BaseModel):
    generate_at: str = "10:00"  # local time, HH:MM
    publish_at: str = "19:30"
    days: list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]] = Field(
        default_factory=lambda: ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    )


class Series(BaseModel):
    series: SeriesMeta
    style: Style
    characters: list[Character] = Field(default_factory=list)
    cast: CastSpec = CastSpec()
    episodes: list[EpisodeSeed] = Field(default_factory=list)
    shots: ShotSpec = ShotSpec()
    models: Models
    music: MusicSpec = MusicSpec()
    title_card: TitleCard = TitleCard()
    caption: CaptionSpec = CaptionSpec()
    budget: Budget = Budget()
    schedule: Schedule = Schedule()

    base_dir: Path = Path(".")

    @model_validator(mode="after")
    def _check(self) -> Series:
        est = self.estimate_reel_cost()
        if est > self.budget.per_reel_usd:
            raise ValueError(
                f"one reel is estimated at ${est:.2f}, above budget.per_reel_usd "
                f"(${self.budget.per_reel_usd:.2f}); lower shots/duration or raise the cap"
            )
        return self

    def estimate_reel_cost(self) -> float:
        """First take: one location plate + n shots + music."""
        n, d = self.shots.count, self.shots.duration_s
        return round(
            self.models.image.estimate() + n * self.models.video.estimate(d) + self.music.cost_per_track,
            4,
        )

    def estimate_cast_cost(self) -> float:
        return round(len(self.characters) * self.cast.candidates * self.models.image.estimate(), 4)

    def resolve(self, p: Path) -> Path:
        return p if p.is_absolute() else (self.base_dir / p)


def load_series(path: Path) -> Series:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw.setdefault("base_dir", Path.cwd())  # asset paths in the YAML are relative to the repo root
    return Series.model_validate(raw)
