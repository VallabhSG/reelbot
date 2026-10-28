"""Runtime settings (secrets + infra) loaded from environment / .env.

Creative settings (style, characters, models, prices) live in the series YAML, see series.py.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- general ---
    series_file: Path = Path("series/example.yaml")
    data_dir: Path = Path("data")
    timezone: str = "Asia/Kolkata"
    dry_run: bool = Field(
        default=False, description="Use stub providers + skip publishing. No API keys needed."
    )

    # --- LLM planner (any OpenAI-compatible endpoint: Groq, OpenRouter, OpenAI, local) ---
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_api_key: str = ""
    llm_model: str = "llama-3.3-70b-versatile"

    # --- Higgsfield (images + video: Soul, Kling, Seedance, PixVerse...) ---
    hf_api_key_id: str = ""
    hf_api_key_secret: str = ""
    hf_base_url: str = "https://api.higgsfield.ai"

    # --- ElevenLabs (music) ---
    elevenlabs_api_key: str = ""

    # --- Cloudflare R2 / any S3-compatible bucket with a public URL (Instagram fetches from here) ---
    s3_endpoint_url: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_bucket: str = ""
    s3_public_base_url: str = ""  # e.g. https://pub-xxxx.r2.dev

    # --- Telegram review bot ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""  # only this chat may approve/reject

    # --- Instagram (Instagram API with Instagram Login) ---
    ig_user_id: str = ""
    ig_access_token: str = ""  # long-lived token; refreshed automatically and stored in the DB
    ig_graph_base_url: str = "https://graph.instagram.com"
    ig_graph_version: str = ""  # e.g. "v23.0"; empty = unversioned path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "reelbot.db"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"


@lru_cache
def get_settings() -> Settings:
    return Settings()
