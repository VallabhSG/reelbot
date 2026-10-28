"""Dry-run providers: produce real local media with Pillow/ffmpeg so the whole pipeline can be
exercised end to end without API keys or spend."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from ..series import Character


def _color(seed: str) -> tuple[int, int, int]:
    h = hashlib.sha256(seed.encode()).digest()
    return h[0], h[1], h[2]


class StubMedia:
    provider_name = "stub"

    def __init__(self, workdir: Path):
        self.workdir = workdir
        workdir.mkdir(parents=True, exist_ok=True)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def image(self, ctx: dict[str, Any], job_id: int | None) -> str:
        self.calls.append(("image", ctx))
        prompt = str(ctx["prompt"])
        out = self.workdir / f"kf_{hashlib.md5(prompt.encode()).hexdigest()[:10]}.png"
        w, h = 1080, 1920
        top, bottom = _color(prompt), _color(prompt[::-1])
        img = Image.new("RGB", (w, h))
        px = ImageDraw.Draw(img)
        for y in range(h):
            t = y / h
            px.line(
                [(0, y), (w, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom, strict=True))
            )
        font = ImageFont.load_default(size=40)
        px.multiline_text((60, 120), _wrap(prompt, 38), font=font, fill=(255, 255, 255))
        img.save(out)
        return out.resolve().as_uri()

    def video(self, ctx: dict[str, Any], job_id: int | None) -> str:
        self.calls.append(("video", ctx))
        src = Path(ctx["image_url"].removeprefix("file://"))
        dur = int(ctx["duration"])
        out = self.workdir / f"{src.stem}_{dur}s.mp4"
        frames = dur * 30
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-loop",
                "1",
                "-i",
                str(src),
                "-vf",
                f"zoompan=z='min(zoom+0.0012,1.25)':d={frames}:s=1080x1920:fps=30",
                "-t",
                str(dur),
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                str(out),
            ],
            check=True,
        )
        return out.resolve().as_uri()

    def reference_urls(self, characters: list[Character]) -> list[str]:
        return [f"file:///stub/{c.name}.png" for c in characters for _ in c.reference_images]


class StubMusic:
    def track(self, prompt: str, seconds: float, dest: Path, job_id: int | None, seed: int) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest = dest.with_suffix(".m4a")
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={220 + seed % 7 * 30}:duration={seconds + 1.5}",
                "-af",
                "volume=0.3",
                "-c:a",
                "aac",
                str(dest),
            ],
            check=True,
        )
        return dest


class StubLLM:
    """Returns a deterministic, schema-valid plan."""

    def json_chat(self, system: str, user: str, temperature: float = 0.9) -> dict[str, Any]:
        n_shots = json.loads(user.split("SHOT_COUNT_JSON=")[1].split("\n")[0])
        return {
            "title": "LAST TRAIN",
            "category": "Metro night driver",
            "emoji": "🚇",
            "logline": "The final train of the night, and one passenger who never gets off.",
            "hook": "Last stop, 1:12 AM.\nShe was still sitting there.",
            "shots": [
                {
                    "keyframe_prompt": f"shot {i + 1}: empty metro carriage at night, neon reflections",
                    "motion_prompt": "slow dolly forward, flickering lights",
                }
                for i in range(n_shots)
            ],
            "music_prompt": "slow lo-fi synth pulse, melancholic, cinematic, instrumental",
        }


def _wrap(text: str, width: int) -> str:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    lines.append(cur)
    return "\n".join(lines)
