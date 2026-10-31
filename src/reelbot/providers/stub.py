"""Dry-run providers: produce real local media with Pillow/ffmpeg so the whole pipeline can be
exercised end to end without API keys or spend."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

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
        refs = ctx.get("image_urls") or [ctx["image_url"]]
        src = Path(unquote(urlparse(refs[-1]).path))  # last ref = the location plate
        if not src.exists():  # e.g. a remote cast URL in dry-run: draw a frame for it
            src = Path(unquote(urlparse(self.image({"prompt": ctx["prompt"][:120]}, job_id)).path))
        dur = int(ctx["duration"])
        out = self.workdir / f"{src.stem}_{hashlib.md5(ctx['prompt'].encode()).hexdigest()[:6]}_{dur}s.mp4"
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

    def take(self, name: str, ctx: dict[str, Any], job_id: int | None) -> str:
        """One-take stub: the opening frame (or a drawn frame) animated for the full length, with a
        clicking sound-effects track so the music/SFX mix is exercised too."""
        self.calls.append(("take", {"model": name, **ctx}))
        start = ctx.get("start_image") or (ctx.get("image_urls") or [""])[0]
        src = Path(unquote(urlparse(start).path)) if start else Path("/nonexistent")
        if not src.exists():
            src = Path(unquote(urlparse(self.image({"prompt": ctx["prompt"][:120]}, job_id)).path))
        dur = int(ctx["duration"])
        out = self.workdir / f"take_{name}_{hashlib.md5(ctx['prompt'].encode()).hexdigest()[:6]}.mp4"
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
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=1200:beep_factor=4:duration={dur}",
                "-vf",
                f"zoompan=z='min(zoom+0.0012,1.25)':d={dur * 30}:s=720x1280:fps=30",
                "-t",
                str(dur),
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(out),
            ],
            check=True,
        )
        return out.resolve().as_uri()

    def reference_urls(self, characters: list[Character]) -> list[str]:
        return [f"file:///stub/{c.name}.png" for c in characters for _ in c.reference_images]


class StubMusic:
    def __init__(self) -> None:
        self.chunks: list[Any] = []  # the composition plans asked for, for tests

    def track(
        self,
        prompt: str,
        seconds: float,
        dest: Path,
        job_id: int | None,
        seed: int,
        chunks: list[dict[str, Any]] | None = None,
    ) -> Path:
        self.chunks.append(chunks)
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
    """Returns a deterministic plan that follows the storyboard rules."""

    SIZES = ["WS", "MCU", "MS", "ECU", "EWS", "CU", "MS", "WS"]

    def json_chat(self, system: str, user: str, temperature: float = 0.9) -> dict[str, Any]:
        from ..storyboard import default_beats

        if "NOTEBOOK" in system:  # the lessons keeper: one lesson per owner note, in order
            notes = [ln.split(': "', 1)[1].rstrip('"') for ln in user.splitlines() if ': "' in ln]
            return {
                "lessons": [{"text": f"Fix: {n}", "evidence": "owner note"} for n in notes][:10]
                or [{"text": "Open on motion in the first second.", "evidence": "stub"}]
            }
        if "PRODUCTION DESIGNER" in system:  # the director's pass: set + blocking
            lo = int(re.findall(r"set in (\d+)-", user)[0])
            lb = int(re.findall(r"blocking in (\d+)-", user)[0])
            return {
                "set": _filler("Cold white tiles line the curved back wall under a humming strip light.", lo),
                "blocking": _filler("She stands at x 30% in the lower third, facing right.", lb),
            }
        if "SHOT DIRECTOR" in system:  # the director's pass: one shot in full
            lo = int(re.findall(r"in (\d+)-(\d+) words:", user)[-1][0])
            n = re.findall(r"WRITE SHOT (\d+)", user)[0]
            return {"detail": _filler(f"Shot {n}: a 35 mm lens at waist height, slow push-in.", lo)}
        if "FIRST-TIME VIEWER" in system:  # the story critic
            return {
                "summary": "she makes the train, the cat was faster",
                "confusions": [],
                "clear": True,
                "score": 9,
            }

        n_shots = json.loads(user.split("SHOT_COUNT_JSON=")[1].split("\n")[0])
        beats = default_beats(n_shots)
        talks = "DIALOGUE BOOKENDS" in system
        section = system.split("RECURRING CAST", 1)[-1].split("\n\n", 1)[0]  # only the cast list
        cast = [ln[2:].split(":")[0] for ln in section.splitlines() if ln.startswith("- ") and ":" in ln]
        cast = [c for c in cast if c and c[0].isupper() and " " not in c][:2]
        return {
            "title": "LAST TRAIN",
            "category": "Metro night driver",
            "emoji": "🚇",
            "logline": "The final train of the night, and one passenger who never gets off.",
            "hook": "Last stop, 1:12 AM.\nShe was still sitting there.",
            "location": {
                "name": "night metro platform",
                "plate_prompt": "an empty underground metro platform at night, wet tiles, cold fluorescent light",
            },
            "winning_move": "she slides through the closing doors on her knees",
            "direction": "left to right",
            "motif": "a red hair tie on her wrist",
            "start_state": "the train waits with its doors open at the far end of the platform; she is at the "
            "foot of the stairs, ten metres away, phone in hand",
            "opening_frame": "she stands at the platform edge, phone in hand, doors open behind her",
            "shots": [
                {
                    "beat": beats[i],
                    "seconds": 3 if beats[i] == "climax" else 2,
                    "size": self.SIZES[i % len(self.SIZES)],
                    "angle": "low angle" if i % 2 else "eye level",
                    "camera": "slow push-in",
                    "cast": cast[:1] if i % 2 == 0 else cast[:2],
                    "action": f"beat {i + 1}: she sprints for the closing doors and slides through",
                    "line": ("Ten seconds. Easy." if i == 0 else "Wrong train." if i == n_shots - 1 else "")
                    if talks and cast
                    else "",
                    "speaker": cast[0] if talks and cast and i in (0, n_shots - 1) else "",
                    "ends": f"state after beat {i + 1}",
                    "sound": "sneakers squeak on wet tiles",
                    "light": "cold key light from screen-left, 5600K",
                }
                for i in range(n_shots)
            ],
            "music_prompt": "slow lo-fi synth pulse, melancholic, cinematic, instrumental",
        }


def _filler(sentence: str, words: int) -> str:
    """A deterministic paragraph of at least `words` words."""
    out: list[str] = []
    while len(out) < words:
        out += sentence.split()
    return " ".join(out)


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
