"""Background music: ElevenLabs Music API, a local library of tracks you own, or none."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Protocol

from ..budget import BudgetGuard
from ..series import MusicSpec

AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}


class MusicProvider(Protocol):
    def track(
        self, prompt: str, seconds: float, dest: Path, job_id: int | None, seed: int
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
        self.spec = spec
        self.guard = guard

    def track(self, prompt: str, seconds: float, dest: Path, job_id: int | None, seed: int) -> Path:
        self.guard.check(self.spec.cost_per_track, job_id)
        kwargs: dict = {"prompt": prompt, "music_length_ms": int(seconds * 1000) + 1500}
        if self.spec.model_id:
            kwargs["model_id"] = self.spec.model_id
        audio = self.client.music.compose(**kwargs)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as f:
            if isinstance(audio, bytes | bytearray):
                f.write(audio)
            else:
                for chunk in audio:
                    f.write(chunk)
        self.guard.record("elevenlabs", "music.compose", self.spec.cost_per_track, job_id)
        return dest


class LibraryMusic:
    """Pick a track from a folder of music you have rights to (cheapest option to start)."""

    def __init__(self, folder: Path):
        self.tracks = sorted(p for p in folder.glob("*") if p.suffix.lower() in AUDIO_EXT)
        if not self.tracks:
            raise ValueError(f"no audio files in {folder}")

    def track(self, prompt: str, seconds: float, dest: Path, job_id: int | None, seed: int) -> Path:
        return random.Random(seed).choice(self.tracks)


class NoMusic:
    def track(self, prompt: str, seconds: float, dest: Path, job_id: int | None, seed: int) -> None:
        return None
