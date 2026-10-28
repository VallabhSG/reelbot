"""ffmpeg assembly (stands in for the CapCut edit): normalise shots to 1080x1920@30, concat, overlay
the title card, lay music under it with fades + loudness normalisation, export a Reels-ready MP4."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
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
    title_png: Path
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
    cmd += ["-loop", "1", "-t", f"{total}", "-i", str(p.title_png)]
    if p.music:
        cmd += ["-i", str(p.music)]
    else:
        cmd += ["-f", "lavfi", "-t", f"{total}", "-i", "anullsrc=r=48000:cl=stereo"]
    title_idx, audio_idx = n, n + 1

    parts = []
    for i, d in enumerate(p.durations):
        parts.append(
            f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
            f"fps={FPS},setsar=1,trim=duration={d},setpts=PTS-STARTPTS[v{i}]"
        )
    parts.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vcat]")

    t0 = max(0.0, p.title.show_from)
    t1 = min(total, p.title.show_until)
    fade = min(0.35, max(0.05, (t1 - t0) / 4))
    parts.append(
        f"[{title_idx}:v]format=rgba,fade=t=in:st={t0}:d={fade}:alpha=1,"
        f"fade=t=out:st={t1 - fade:.3f}:d={fade}:alpha=1[tc]"
    )
    parts.append(f"[vcat][tc]overlay=0:0:enable='between(t,{t0},{t1})',format=yuv420p[vout]")

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
