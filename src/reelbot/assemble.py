"""ffmpeg assembly (stands in for the CapCut edit).

v2: normalise shots to 1080x1920@30, concat, optional title overlay, music with fades + loudnorm.
v3: frame the single take for Reels, mix music over the model's own sound effects, dip the music
    before the climax, an anime impact frame on the climax cut, master to -14 LUFS."""

from __future__ import annotations

import json
import math
import random
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .series import TitleCard

W, H, FPS = 1080, 1920, 30


class FFmpegError(RuntimeError):
    pass


def probe_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(json.loads(r.stdout)["format"]["duration"])


@dataclass
class AssemblyPlan:
    clips: list[Path]
    durations: list[float]  # seconds used from each clip
    title_png: Path | None  # None: no title burned into the video (poster cover only)
    title: TitleCard
    music: Path | None
    music_volume_db: float
    out: Path

    @property
    def total(self) -> float:
        return round(sum(self.durations), 3)


def build_command(p: AssemblyPlan) -> list[str]:
    n = len(p.clips)
    total = p.total
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for clip in p.clips:
        cmd += ["-i", str(clip)]
    next_idx = n
    title_idx = None
    if p.title_png:
        cmd += ["-loop", "1", "-t", f"{total}", "-i", str(p.title_png)]
        title_idx, next_idx = next_idx, next_idx + 1
    if p.music:
        cmd += ["-i", str(p.music)]
    else:
        cmd += ["-f", "lavfi", "-t", f"{total}", "-i", "anullsrc=r=48000:cl=stereo"]
    audio_idx = next_idx

    parts = []
    for i, d in enumerate(p.durations):
        parts.append(
            f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
            f"fps={FPS},setsar=1,trim=duration={d},setpts=PTS-STARTPTS[v{i}]"
        )
    parts.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vcat]")

    if title_idx is not None:
        t0 = max(0.0, p.title.show_from)
        t1 = min(total, p.title.show_until)
        fade = min(0.35, max(0.05, (t1 - t0) / 4))
        parts.append(
            f"[{title_idx}:v]format=rgba,fade=t=in:st={t0}:d={fade}:alpha=1,"
            f"fade=t=out:st={t1 - fade:.3f}:d={fade}:alpha=1[tc]"
        )
        parts.append(f"[vcat][tc]overlay=0:0:enable='between(t,{t0},{t1})',format=yuv420p[vout]")
    else:
        parts.append("[vcat]format=yuv420p[vout]")

    fade_out = min(1.5, total / 4)
    parts.append(
        f"[{audio_idx}:a]atrim=0:{total},asetpts=PTS-STARTPTS,aresample=48000,"
        f"afade=t=in:d=0.3,afade=t=out:st={total - fade_out:.3f}:d={fade_out:.3f},"
        f"volume={p.music_volume_db}dB,loudnorm=I=-14:TP=-1.5:LRA=11[aout]"
    )

    cmd += [
        "-filter_complex",
        ";".join(parts),
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-profile:v",
        "high",
        "-r",
        str(FPS),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-ar",
        "48000",
        "-movflags",
        "+faststart",
        "-t",
        f"{total}",
        str(p.out),
    ]
    return cmd


def assemble(p: AssemblyPlan) -> Path:
    p.out.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(build_command(p), capture_output=True, text=True)
    if r.returncode != 0:
        raise FFmpegError(r.stderr[-2000:])
    return p.out


# ------------------------------------------------------------------ v3: one take
def has_audio(path: Path) -> bool:
    r = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    return bool(r.stdout.strip())


@dataclass
class TakeAssembly:
    """The cuts are already inside the take; this only frames it for Reels and builds the mix the
    reference reels use: music-led, the model's own sound effects underneath, a music dip right
    before the climax that slams back on it, mastered to -14 LUFS."""

    take: Path
    seconds: float
    music: Path | None
    music_volume_db: float
    sfx_volume_db: float
    dips: list[tuple[float, float]]  # music drops out (pre-climax tension, the quiet beat before the end)
    out: Path
    # dialogue bookends: during these ranges the model's audio (the spoken line) comes up to full
    # level and the music ducks; subtitles are (png, start, end) overlays
    speech: list[tuple[float, float]] = field(default_factory=list)
    subtitles: list[tuple[Path, float, float]] = field(default_factory=list)
    flashes: list[float] = field(default_factory=list)  # 2-frame white flash frames (on the big cut)
    # anime "impact frames" (start, end): the picture goes high-contrast black & white under radial
    # speed lines for a few frames, the way the top reels hit their climax
    impacts: list[tuple[float, float]] = field(default_factory=list)
    speed_lines: Path | None = None


IMPACT_FRAMES = 6  # ~0.2 s at 30 fps


def render_speed_lines(out: Path, seed: int = 7) -> Path:
    """Radial manga speed lines on a transparent frame, with a clear centre for the action."""
    from PIL import Image, ImageDraw

    out.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    rnd = random.Random(seed)
    cx, cy, far = W / 2, H * 0.45, math.hypot(W, H)
    for _ in range(150):
        a, w = rnd.uniform(0, 2 * math.pi), rnd.uniform(0.004, 0.018)
        r0 = rnd.uniform(0.35, 0.56) * W
        d.polygon(
            [
                (cx + math.cos(a) * r0, cy + math.sin(a) * r0),
                (cx + math.cos(a - w) * far, cy + math.sin(a - w) * far),
                (cx + math.cos(a + w) * far, cy + math.sin(a + w) * far),
            ],
            fill=(255, 255, 255, rnd.randint(200, 255)),
        )
    img.save(out)
    return out


SPEECH_GAIN_DB = 2.0  # spoken lines sit this far above unity in the mix
MUSIC_UNDER_SPEECH = 0.22  # music level (linear) while someone talks, ~-13 dB


def _between(a: float, b: float) -> str:
    return f"between(t,{a:.3f},{b:.3f})"


def build_take_command(p: TakeAssembly, sfx: bool) -> list[str]:
    total = p.seconds
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(p.take)]
    if p.music:
        cmd += ["-i", str(p.music)]
    else:
        cmd += ["-f", "lavfi", "-t", f"{total}", "-i", "anullsrc=r=48000:cl=stereo"]
    for png, _, _ in p.subtitles:
        cmd += ["-loop", "1", "-t", f"{total}", "-i", str(png)]
    impacts = p.impacts if p.speed_lines else []
    if impacts:
        cmd += ["-loop", "1", "-t", f"{total}", "-i", str(p.speed_lines)]
    base = (
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},setsar=1,"
        f"trim=duration={total},setpts=PTS-STARTPTS"
    )
    for t0 in p.flashes:  # "flash frames": two white frames right on the cut
        base += f",drawbox=c=white@0.9:t=fill:enable='{_between(t0, t0 + 2 / FPS)}'"
    for a, b in impacts:  # impact frame: hard black & white under the speed lines
        on = _between(a, b)
        base += f",hue=s=0:enable='{on}',eq=contrast=2.2:brightness=-0.15:enable='{on}'"
    if impacts:  # the speed lines are the last input, after the subtitle images
        on = "+".join(_between(a, b) for a, b in impacts)
        parts = [base + "[vb]", f"[vb][{len(p.subtitles) + 2}:v]overlay=0:0:enable='{on}'[v0]"]
    else:
        parts = [base + "[v0]"]
    for k, (_, a, b) in enumerate(p.subtitles):
        parts.append(f"[v{k}][{k + 2}:v]overlay=0:0:enable='{_between(a, b)}'[v{k + 1}]")
    parts.append(f"[v{len(p.subtitles)}]format=yuv420p[vout]")
    fade_out = min(1.2, total / 5)
    music = (
        f"[1:a]atrim=0:{total},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,"
        f"afade=t=in:d=0.2,afade=t=out:st={total - fade_out:.3f}:d={fade_out:.3f},"
        f"volume={p.music_volume_db}dB"
    )
    for a, b in p.dips if p.music else []:
        music += f",volume=volume=0.12:enable='{_between(a, b)}'"
    if sfx:
        for a, b in p.speech:
            music += f",volume=volume={MUSIC_UNDER_SPEECH}:enable='{_between(a, b)}'"
        model = (
            f"[0:a]atrim=0:{total},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,"
            f"volume={p.sfx_volume_db}dB"
        )
        lift = 10 ** ((SPEECH_GAIN_DB - p.sfx_volume_db) / 20)  # undo the SFX trim while a line plays
        for a, b in p.speech:
            model += f",volume=volume={lift:.3f}:enable='{_between(a, b)}'"
        parts.append(music + "[mus]")
        parts.append(model + "[sfx]")
        parts.append("[mus][sfx]amix=inputs=2:normalize=0:duration=first,loudnorm=I=-14:TP=-1.5:LRA=11[aout]")
    else:
        parts.append(music + ",loudnorm=I=-14:TP=-1.5:LRA=11[aout]")
    cmd += [
        "-filter_complex",
        ";".join(parts),
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-profile:v",
        "high",
        "-r",
        str(FPS),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-ar",
        "48000",
        "-movflags",
        "+faststart",
        "-t",
        f"{total}",
        str(p.out),
    ]
    return cmd


def detect_cuts(path: Path, threshold: float = 0.3) -> list[float]:
    """Hard-cut timestamps inside a generated take (the model decides the exact frame)."""
    r = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(path),
            "-vf",
            f"select='gt(scene,{threshold})',showinfo",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    )
    return [float(m) for m in re.findall(r"pts_time:([0-9.]+)", r.stderr)]


def snap(t: float, cuts: list[float], window: float = 0.7) -> float:
    """The planned cut time, moved to the nearest real cut within `window` seconds."""
    near = [c for c in cuts if abs(c - t) <= window]
    return min(near, key=lambda c: abs(c - t)) if near else t


def assemble_take(p: TakeAssembly) -> Path:
    p.out.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(build_take_command(p, has_audio(p.take)), capture_output=True, text=True)
    if r.returncode != 0:
        raise FFmpegError(r.stderr[-2000:])
    return p.out
