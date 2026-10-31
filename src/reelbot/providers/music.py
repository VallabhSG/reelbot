"""Background music: ElevenLabs Music API, a local library of tracks you own, or none."""

from __future__ import annotations

import logging
import random
from pathlib import Path
from typing import Any, Protocol

import httpx

from ..budget import BudgetGuard
from ..series import MusicSpec

AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
API = "https://api.elevenlabs.io"
TAIL_MS = 1500  # a little extra after the reel, so the fade-out never runs out of music

log = logging.getLogger(__name__)


class MusicProvider(Protocol):
    def track(
        self,
        prompt: str,
        seconds: float,
        dest: Path,
        job_id: int | None,
        seed: int,
        chunks: list[dict[str, Any]] | None = None,
    ) -> Path | None: ...


class ElevenLabsMusic:
    """elevenlabs.music.compose(prompt, music_length_ms) — paid ElevenLabs plans only.

    Prompts naming real artists/songs are rejected by the API, so the planner is told not to.
    """

    def __init__(self, api_key: str, spec: MusicSpec, guard: BudgetGuard):
        if not api_key:
            raise ValueError("ELEVENLABS_API_KEY is required for music.provider=elevenlabs")
        try:
            from elevenlabs.client import ElevenLabs
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("install the music extra: pip install 'reelbot[music]'") from e
        self.client = ElevenLabs(api_key=api_key)
        self.api_key = api_key
        self.spec = spec
        self.guard = guard
        self.http = httpx.Client(timeout=httpx.Timeout(300.0, connect=15.0))

    def track(
        self,
        prompt: str,
        seconds: float,
        dest: Path,
        job_id: int | None,
        seed: int,
        chunks: list[dict[str, Any]] | None = None,
    ) -> Path:
        self.guard.check(self.spec.cost_per_track, job_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        audio: Any = None
        if chunks and self.spec.composition:
            try:
                audio = self._compose_plan(chunks, seed)
            except Exception as e:  # a refused plan (model, plan tier) still gets a track
                log.warning("music composition plan failed (%s); composing from the prompt", e)
        if audio is None:
            kwargs: dict = {
                "prompt": prompt,
                "music_length_ms": int(seconds * 1000) + TAIL_MS,
                "force_instrumental": True,
            }
            if self.spec.model_id:
                kwargs["model_id"] = self.spec.model_id
            audio = self.client.music.compose(**kwargs)
        with dest.open("wb") as f:
            if isinstance(audio, bytes | bytearray):
                f.write(audio)
            else:
                for chunk in audio:
                    f.write(chunk)
        self.guard.record("elevenlabs", "music.compose", self.spec.cost_per_track, job_id)
        return dest

    def _compose_plan(self, chunks: list[dict[str, Any]], seed: int) -> bytes:
        """One section per story beat (music_plan.py): the newer music models take a composition
        plan instead of a prompt, so the score builds and hits where the reel does."""
        chunks = [dict(c) for c in chunks]
        chunks[-1]["duration_ms"] += TAIL_MS
        r = self.http.post(
            f"{API}/v1/music",
            params={"output_format": "mp3_44100_128"},
            headers={"xi-api-key": self.api_key},
            json={"composition_plan": {"chunks": chunks}, "model_id": self.spec.plan_model, "seed": seed},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code}: {r.text[:300]}")
        if not r.content:
            raise RuntimeError("empty audio")
        return r.content


class LibraryMusic:
    """Pick a track from a folder of music you have rights to (cheapest option to start)."""

    def __init__(self, folder: Path):
        self.tracks = sorted(p for p in folder.glob("*") if p.suffix.lower() in AUDIO_EXT)
        if not self.tracks:
            raise ValueError(f"no audio files in {folder}")

    def track(
        self,
        prompt: str,
        seconds: float,
        dest: Path,
        job_id: int | None,
        seed: int,
        chunks: list[dict[str, Any]] | None = None,
    ) -> Path:
        return random.Random(seed).choice(self.tracks)


class NoMusic:
    def track(
        self,
        prompt: str,
        seconds: float,
        dest: Path,
        job_id: int | None,
        seed: int,
        chunks: list[dict[str, Any]] | None = None,
    ) -> None:
        return None
