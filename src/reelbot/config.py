"""Runtime settings (secrets + infra) loaded from environment / .env.

Creative settings (style, characters, models, prices) live in the series YAML, see series.py.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
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
    llm_model: str = "openai/gpt-oss-120b"  # Groq's replacement for the retired llama-3.3-70b-versatile

    # --- Higgsfield (images + video: Soul, Kling, Seedance, PixVerse...) ---
    # Either the single value the console's "Copy API key" gives ("<id>:<secret>") or the two parts.
    hf_api_key: str = ""
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
    s3_state_bucket: str = ""  # PRIVATE bucket for the SQLite DB + in-flight renders (holds the IG token)
    s3_public_base_url: str = ""  # e.g. https://pub-xxxx.r2.dev

    # --- Telegram review bot ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""  # only this chat may approve/reject

    # --- Instagram (Instagram API with Instagram Login) ---
    ig_user_id: str = ""
    ig_access_token: str = ""  # long-lived token; refreshed automatically and stored in the DB
    ig_graph_base_url: str = "https://graph.instagram.com"
    ig_graph_version: str = ""  # e.g. "v23.0"; empty = unversioned path

    @model_validator(mode="after")
    def _split_hf_key(self) -> Settings:
        key = self.hf_api_key.strip()
        if key and not (self.hf_api_key_id and self.hf_api_key_secret):
            if ":" not in key:
                raise ValueError(
                    "HF_API_KEY must look like <key id>:<key secret> (as the Higgsfield console's "
                    "'Copy API key' button gives it); set HF_API_KEY_ID and HF_API_KEY_SECRET instead "
                    "if your console shows them separately"
                )
            self.hf_api_key_id, self.hf_api_key_secret = (p.strip() for p in key.split(":", 1))
        return self

    @property
    def db_path(self) -> Path:
        return self.data_dir / "reelbot.db"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"


@lru_cache
def get_settings() -> Settings:
    return Settings()
