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
    speaks: bool = True  # False: never gets a spoken line (e.g. the cat)


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
    # A fixed gag script (v3). When given, the planner must follow it and only adds shots, camera,
    # timing and sound: cause -> exaggerated effect -> payoff, each literal and drawable.
    location: str = ""
    stake: str = ""  # hook/escalate: the relatable situation (and any opening line)
    move: str = ""  # the winning move: one physical action
    spectacle: str = ""  # how the feeling is blown up into anime spectacle
    payoff: str = ""  # the final image / punchline (and any closing line)
    # A famous movie moment to stage it like: its camera language, framing and timing only (the
    # planner never names the film or copies its characters, costumes, props, lines or music)
    homage: str = ""
    # Optional motion/camera reference clip URL (Seedance 2.5 `video_urls`). Use clips you're
    # allowed to use: your own footage, licensed stock, or an earlier reel of ours.
    reference_video: str = ""

    @property
    def scripted(self) -> bool:
        return bool(self.stake and self.move and self.payoff)


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
    label: str = ""  # human name shown in Telegram, e.g. "Kling 3.0"
    max_prompt_chars: int = 0  # the endpoint's prompt limit (0 = none); v3 prompts shrink to fit
    # v3: True = the model starts from the composed opening frame (image-to-video); False = it gets
    # the location plate + character sheets as references (reference-to-video, image 1 = location)
    start_frame: bool = False

    def estimate(self, seconds: float = 0.0) -> float:
        return round(self.cost_per_call + self.cost_per_second * seconds, 4)


class Models(BaseModel):
    image: ModelSpec
    video: ModelSpec  # v2: one call per shot
    # v3: models that render the WHOLE reel in one call (cuts inside the generation), by short name
    takes: dict[str, ModelSpec] = Field(default_factory=dict)


class TakeSpec(BaseModel):
    """v3: the whole reel as ONE generation with hard cuts inside it, so characters, light and action
    carry across the cuts. The opening frame is composed first (cast sheets + location plate) and the
    video starts from it, so characters sit in the scene instead of being pasted on."""

    enabled: bool = False
    seconds: int = Field(15, ge=4, le=30)
    shots: int = Field(6, ge=2, le=10)
    min_shot_s: int = Field(1, ge=1)
    max_shot_s: int = Field(4, ge=1)
    model: str = ""  # key in models.takes used for every reel (a bake-off winner overrides it)
    bakeoff: list[str] = Field(default_factory=list)  # keys rendered side by side by `reelbot bakeoff`
    # "bookends": a few short spoken lines in the opening and at the twist, wordless in between
    # (the format of the top reels in this niche); "none": fully wordless
    dialogue: Literal["none", "bookends"] = "none"
    max_lines: int = Field(3, ge=0, le=6)
    subtitles: bool = True  # burn spoken lines in as subtitles (our font, exact timing)
    sfx_volume_db: float = -8.0  # the model's own sound effects, under the music (music-led mix)
    music_volume_db: float = 0.0
    climax_dip_s: float = 0.8  # music dips this long right before the climax and slams back on it
    quiet_before_twist_s: float = 0.6  # "one quiet second before the ending" after the burst
    flash_on_climax: bool = True  # a 2-frame white flash frame on the cut into the climax
    impact_frame: bool = True  # then ~0.2 s anime impact frame: black & white under speed lines
    music_bpm: int = 120  # cuts fall on whole seconds, which are beats at 120 BPM

    @model_validator(mode="after")
    def _fits(self) -> TakeSpec:
        if not self.shots * self.min_shot_s <= self.seconds <= self.shots * self.max_shot_s:
            raise ValueError(
                f"{self.shots} shots of {self.min_shot_s}-{self.max_shot_s}s can't fill {self.seconds}s"
            )
        return self


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
    subtitle_font: Path | None = None  # spoken-line subtitles (default: DejaVu Sans Bold)
    subtitle_color: str = "#FFD84A"  # anime-subtitle yellow
    cover: bool = True  # poster cover (best frame + title) uploaded as the Reels cover image
    cover_offset_ms: int = 1200  # fallback cover frame if no poster cover is used


class CaptionSpec(BaseModel):
    template: str = "{header}\n\n{hook}\n\n{credits}\n\n{hashtags}"
    credits: str = ""
    hashtags: list[str] = Field(default_factory=list)
    # Affiliate link (e.g. your Higgsfield one). Empty = nothing is added. Instagram captions can't
    # hold a clickable link, so they point to the bio; YouTube and Facebook get the link itself.
    # Added when a reel is posted, so reels planned earlier get it too.
    affiliate_url: str = ""
    affiliate_instagram: str = "🎬 Made with Higgsfield · affiliate link in bio"
    affiliate_link_text: str = "🎬 Made with Higgsfield (affiliate link): {url}"


class YouTubeSpec(BaseModel):
    """YouTube Shorts: the same reel, posted right after Instagram once YOUTUBE_* credentials exist.
    Placeholders: {hook_line} (the caption hook's first line), {hook}, {title}, {emoji}, {category},
    {n}, {series}, {credits}, {hashtags}."""

    enabled: bool = True
    title: str = "{hook_line} {emoji}"
    description: str = "{series} · Episode {n}: {title}\n\n{hook}\n\n{credits}\n\n{hashtags} #shorts"
    category_id: str = "1"  # Film & Animation
    made_for_kids: bool = False
    # YouTube's "altered or synthetic content" label is required for realistic footage; obvious
    # animation doesn't need it. Set true to label every Short anyway.
    synthetic_media: bool = False


class FacebookSpec(BaseModel):
    """Facebook Reels: the same reel on a Facebook Page, posted right after Instagram once FB_PAGE_ID
    and FB_PAGE_TOKEN exist. The description is the Instagram caption; title placeholders as YouTube."""

    enabled: bool = True
    title: str = "{hook_line} {emoji}"
    # Meta's "AI info" label. Required for photorealistic AI video; optional for animation (the
    # caption already says #aianimation). Set true to label every reel anyway.
    ai_label: bool = False


class Budget(BaseModel):
    monthly_usd: float = 40.0
    per_reel_usd: float = 3.0


Platform = Literal["instagram", "youtube", "facebook"]


class Schedule(BaseModel):
    generate_at: str = "10:00"  # local time, HH:MM
    publish_at: str = "19:30"
    days: list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]] = Field(
        default_factory=lambda: ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    )
    # Spread each reel over several days, one platform per day from the approval slot:
    # [instagram, youtube, facebook] = day 0, +1, +2. Empty = every platform at once.
    stagger: list[Platform] = Field(default_factory=list)
    # Shift that order every week so each platform gets to try every weekday (a 3-week cycle);
    # the weekly report then shows which days each platform does best on.
    rotate: bool = False


class Series(BaseModel):
    series: SeriesMeta
    style: Style
    characters: list[Character] = Field(default_factory=list)
    cast: CastSpec = CastSpec()
    episodes: list[EpisodeSeed] = Field(default_factory=list)
    # famous movie moments to stage unscripted episodes like (film language only, never the film's
    # characters, lines or music); the planner picks the one that fits the gag
    homages: list[str] = Field(default_factory=list)
    shots: ShotSpec = ShotSpec()
    take: TakeSpec = TakeSpec()
    models: Models
    music: MusicSpec = MusicSpec()
    title_card: TitleCard = TitleCard()
    caption: CaptionSpec = CaptionSpec()
    youtube: YouTubeSpec = YouTubeSpec()
    facebook: FacebookSpec = FacebookSpec()
    budget: Budget = Budget()
    schedule: Schedule = Schedule()

    base_dir: Path = Path(".")

    @model_validator(mode="after")
    def _check(self) -> Series:
        if self.take.enabled:
            names = [n for n in [self.take.model, *self.take.bakeoff] if n]
            if not names:
                raise ValueError("take.enabled needs take.model (a key in models.takes)")
            unknown = [n for n in names if n not in self.models.takes]
            if unknown:
                raise ValueError(f"take model(s) {unknown} not in models.takes {sorted(self.models.takes)}")
        for name in self.take_model_names() or [None]:
            est = self.estimate_reel_cost(name)
            if est > self.budget.per_reel_usd:
                raise ValueError(
                    f"one reel{f' on {name}' if name else ''} is estimated at ${est:.2f}, above "
                    f"budget.per_reel_usd (${self.budget.per_reel_usd:.2f}); shorten it or raise the cap"
                )
        return self

    @property
    def shot_count(self) -> int:
        return self.take.shots if self.take.enabled else self.shots.count

    @property
    def reel_seconds(self) -> int:
        return self.take.seconds if self.take.enabled else self.shots.count * self.shots.duration_s

    def take_model_names(self) -> list[str]:
        if not self.take.enabled:
            return []
        return list(dict.fromkeys(n for n in [self.take.model, *self.take.bakeoff] if n))

    def estimate_reel_cost(self, take_model: str | None = None) -> float:
        """First take. v2: location plate + n shots + music. v3: plate + opening frame + one take + music."""
        if self.take.enabled:
            spec = self.models.takes[take_model or self.take.model]
            frames = 2 if spec.start_frame else 1  # location plate (+ the opening frame)
            return round(
                frames * self.models.image.estimate()
                + spec.estimate(self.take.seconds)
                + self.music.cost_per_track,
                4,
            )
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
