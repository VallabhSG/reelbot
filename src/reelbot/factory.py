"""Builds a Pipeline from settings: live providers, or stubs when DRY_RUN=true."""

from __future__ import annotations

from dataclasses import dataclass

from .budget import BudgetGuard
from .config import Settings
from .db import DB
from .facebook import DryRunFacebook, FacebookPublisher
from .instagram import DryRunPublisher, InstagramPublisher
from .pipeline import Pipeline
from .planner import Planner
from .providers.higgsfield import HiggsfieldClient, HiggsfieldMedia
from .providers.llm import LLMClient
from .providers.music import ElevenLabsMusic, LibraryMusic, NoMusic
from .providers.openrouter_video import OpenRouterVideo
from .providers.stub import StubLLM, StubMedia, StubMusic
from .series import Series, load_series
from .storage import LocalStorage, S3Storage
from .telegram import ConsoleNotifier, Telegram
from .youtube import DryRunYouTube, YouTubePublisher


@dataclass
class App:
    settings: Settings
    series: Series
    db: DB
    guard: BudgetGuard
    pipeline: Pipeline


def build(settings: Settings, series: Series | None = None) -> App:
    series = series or load_series(settings.series_file)
    db = DB(settings.db_path)
    guard = BudgetGuard(db, series.budget, settings.timezone)

    if settings.dry_run:
        llm = StubLLM()
        media = StubMedia(settings.data_dir / "stub")
        music = StubMusic() if series.music.provider != "none" else NoMusic()
        notifier = ConsoleNotifier()
        storage = LocalStorage()
        publisher = DryRunPublisher()
        youtube = DryRunYouTube()
        facebook = DryRunFacebook()
    else:
        llm = LLMClient(
            settings.llm_base_url,
            settings.llm_api_key,
            settings.llm_model,
            reasoning_effort=settings.llm_reasoning_effort,
        )
        media = HiggsfieldMedia(
            HiggsfieldClient(settings.hf_api_key_id, settings.hf_api_key_secret, settings.hf_base_url),
            series.models,
            guard,
            series.base_dir,
            openrouter=(
                OpenRouterVideo(
                    settings.openrouter_api_key,
                    settings.openrouter_base_url,
                    work_dir=settings.data_dir / "openrouter",
                )
                if series.uses_provider("openrouter")
                else None
            ),
        )
        if series.music.provider == "elevenlabs":
            music = ElevenLabsMusic(settings.elevenlabs_api_key, series.music, guard)
        elif series.music.provider == "library":
            music = LibraryMusic(series.resolve(series.music.library_dir))
        else:
            music = NoMusic()
        notifier = Telegram(settings.telegram_bot_token, settings.telegram_chat_id)
        storage = S3Storage(settings)
        publisher = InstagramPublisher(
            settings.ig_user_id,
            settings.ig_access_token,
            db,
            base_url=settings.ig_graph_base_url,
            version=settings.ig_graph_version,
        )
        youtube = (
            YouTubePublisher(
                settings.youtube_client_id, settings.youtube_client_secret, settings.youtube_refresh_token
            )
            if settings.youtube_enabled
            else None
        )
        facebook = (
            FacebookPublisher(settings.fb_page_id, settings.fb_page_token, settings.fb_graph_version)
            if settings.facebook_enabled
            else None
        )

    pipeline = Pipeline(
        series=series,
        db=db,
        jobs_dir=settings.jobs_dir,
        planner=Planner(llm, series),
        media=media,
        music=music,
        guard=guard,
        notifier=notifier,
        storage=storage,
        publisher=publisher,
        youtube=youtube if series.youtube.enabled else None,
        facebook=facebook if series.facebook.enabled else None,
    )
    return App(settings, series, db, guard, pipeline)
