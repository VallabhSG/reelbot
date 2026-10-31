"""Orchestration.

cast (once):  character sheets -> Telegram -> you pick one per character -> reused forever
v2 episode:   storyboard -> location plate -> N separate shots (reference-to-video) -> music -> cut
v3 episode:   continuous storyboard -> location plate -> opening frame (cast sheets + plate, painted
              together) -> ONE generation of the whole reel with the cuts inside it, sound effects
              on -> music -> mix (music-led, dip before the climax) -> poster cover
then:         Telegram review -> Instagram (with the poster as the Reels cover)
learning:     your verdicts + notes and each reel's Instagram numbers -> lessons -> next storyboard
bake-off:     the same v3 storyboard, opening frame and music rendered on several video models, so
              the only difference you judge is the model
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from .assemble import (
    FPS,
    IMPACT_FRAMES,
    AssemblyPlan,
    TakeAssembly,
    assemble,
    assemble_take,
    detect_cuts,
    probe_duration,
    render_speed_lines,
    snap,
)
from .budget import BudgetExceeded, BudgetGuard
from .db import DB
from .director import Director
from .facebook import reel_url
from .learn import Learner, planner_guidance, rematch_brief, rematch_candidate
from .models import EpisodePlan, JobStatus
from .music_plan import music_chunks
from .planner import Planner, wins_this_episode
from .providers import MediaProvider
from .providers._http import download
from .providers.music import MusicProvider, NoMusic
from .series import EpisodeSeed, Series
from .storage import Storage
from .storyboard import (
    clip_text,
    compose_keyframe_prompt,
    compose_shot_prompt,
    compose_take_prompt,
    episode_cast,
    shot_starts,
)
from .telegram import cast_keyboard, cast_more_keyboard, review_keyboard, take_keyboard
from .titlecard import render_cover, render_subtitle, render_title_card
from .youtube import shorts_url

log = logging.getLogger(__name__)

IG_CAPTION_MAX = 2200
CROSSPOST_TRIES = 3
CROSSPOST_NAMES = {"youtube": "YouTube", "facebook": "Facebook"}
CROSSPOST_OFF = {
    "youtube": "YouTube is off: set YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN",
    "facebook": "Facebook is off: set FB_PAGE_ID and FB_PAGE_TOKEN",
}
IG_HASHTAG_MAX = 5  # Instagram's cap since Dec 2025; tags past it are dropped or hurt distribution
HASHTAG_RE = re.compile(r"(?<![\w&])#\w+")


def limit_hashtags(text: str, n: int = IG_HASHTAG_MAX) -> str:
    """Keep the first n hashtags, drop the rest (captions written before the cap had more)."""
    seen = 0

    def keep(m: re.Match[str]) -> str:
        nonlocal seen
        seen += 1
        return m.group(0) if seen <= n else ""

    out = HASHTAG_RE.sub(keep, text)
    return "\n".join(
        " ".join(ln.split()) if "#" in ln or not ln.strip() else ln for ln in out.splitlines()
    ).strip()


class CastNotReady(RuntimeError):
    pass


def spoken_seconds(line: str) -> float:
    """How long a line takes to say, with slack for the model starting it a beat late."""
    return 1.0 + 0.4 * len(line.split())


class Pipeline:
    def __init__(
        self,
        series: Series,
        db: DB,
        jobs_dir: Path,
        planner: Planner,
        media: MediaProvider,
        music: MusicProvider,
        guard: BudgetGuard,
        notifier: Any,
        storage: Storage,
        publisher: Any,
        youtube: Any = None,
        facebook: Any = None,
    ):
        self.s = series
        self.db = db
        self.jobs_dir = jobs_dir
        self.planner = planner
        self.media = media
        self.music = music
        self.guard = guard
        self.notifier = notifier
        self.storage = storage
        self.publisher = publisher
        self.youtube = youtube  # None = YouTube Shorts off
        self.facebook = facebook  # None = Facebook Reels off
        self.learner = Learner(
            db, series, planner.llm, publisher, notifier, platforms=self.crosspost_platforms, tz=guard.tz
        )
        self.director = Director(planner.llm, series) if planner.llm is not None else None

    # ================================================================== director
    def _directed(self, plan: EpisodePlan, job_id: int | None = None) -> EpisodePlan:
        """The director's pass (director.py): the set, the blocking and every shot written out in
        full, once per episode, before any image is made (the plate and opening frame are drawn
        from the same words). Never blocks the day: on failure the reel renders from the
        storyboard alone."""
        if not (self.s.take.enabled and self.s.take.director and self.director) or plan.world:
            return plan
        try:
            directed = self.director.direct(plan)
        except Exception as e:
            log.exception("director pass failed")
            tag = f"#{job_id}: " if job_id else ""
            self.notifier.send_message(
                f"⚠️ {tag}the director pass failed ({type(e).__name__}: {str(e)[:200]}); "
                "rendering from the storyboard alone."
            )
            return plan
        missing = [i + 1 for i, s in enumerate(directed.shots) if not s.detail]
        if missing:
            log.warning("director: no detail for shot(s) %s; they use the storyboard fields", missing)
        if job_id is not None:
            self.db.update_job(job_id, plan_json=directed.model_dump_json())
        return directed

    # ================================================================== cast
    def cast_urls(self) -> dict[str, str]:
        return {c.name: url for c in self.s.characters if (url := self.db.kv_get(f"cast:{c.name}"))}

    def cast_missing(self) -> list[str]:
        have = self.cast_urls()
        return [c.name for c in self.s.characters if c.name not in have]

    def propose_cast(self, names: list[str] | None = None) -> dict[str, list[str]]:
        """Generate candidate character sheets and send them to Telegram for picking."""
        chars = {c.name: c for c in self.s.characters}
        names = names or self.cast_missing() or list(chars)
        out: dict[str, list[str]] = {}
        d = self.jobs_dir / "cast"
        d.mkdir(parents=True, exist_ok=True)
        for name in names:
            ch = chars[name]
            prompt = self.s.cast.sheet_template.format(description=ch.description, style=self.s.style.bible)
            urls: list[str] = []
            msgs: list[int] = []
            for i in range(self.s.cast.candidates):
                src = self.media.image(
                    {
                        "prompt": prompt,
                        "aspect_ratio": self.s.cast.aspect_ratio,
                        "image_urls": [],
                        "negative_prompt": self.s.style.negative or None,
                    },
                    None,
                )
                local = download(src, d / f"{name}_{datetime.now(UTC):%Y%m%d%H%M%S}_{i}.png")
                _, public = self.storage.put_public(local, f"cast/{name}")  # permanent, model-readable URL
                urls.append(public)
                msgs.append(
                    self.notifier.send_photo(local, f"🎭 {name} · option {i + 1}", cast_keyboard(name, i))
                )
            self.db.kv_set(f"cast_candidates:{name}", json.dumps(urls))
            msgs.append(
                self.notifier.send_message(
                    f"Pick the {name} you like. Every future shot will use that exact sheet.",
                    cast_more_keyboard(name),
                )
            )
            self.db.kv_set(f"cast_msgs:{name}", json.dumps(msgs))
            out[name] = urls
        return out

    def lock_cast(self, name: str, idx: int, reply_to: int | None = None) -> str:
        """Approve option idx (0-based), remove the pick buttons, confirm in Telegram.
        Shared by the ✅ button and `reelbot pick`, so both behave the same."""
        self.approve_cast(name, idx)
        left = self.cast_missing()
        tail = f" Still to pick: {', '.join(left)}." if left else " Cast complete, episodes are unblocked."
        text = f"🎭 {name} locked in (option {idx + 1}).{tail}"
        try:  # the pick is saved; Telegram is only the receipt
            for mid in json.loads(self.db.kv_get(f"cast_msgs:{name}") or "[]"):
                self.notifier.clear_keyboard(mid)
            self.notifier.send_message(text, reply_to=reply_to)
        except Exception:
            log.exception("cast %s locked, but the Telegram confirmation failed", name)
        return text

    def approve_cast(self, name: str, idx: int) -> str:
        raw = self.db.kv_get(f"cast_candidates:{name}")
        if not raw:
            raise ValueError(f"no {name} options to pick from; run cast first")
        urls = json.loads(raw)
        if not 0 <= idx < len(urls):
            raise ValueError(f"{name} option {idx + 1} doesn't exist")
        self.db.kv_set(f"cast:{name}", urls[idx])
        return urls[idx]

    # ================================================================== planning
    def plan_next(self) -> int:
        n = self.db.next_episode_no()
        if n > self.s.series.total_episodes:
            raise RuntimeError(f"series complete ({self.s.series.total_episodes} episodes)")
        guidance = planner_guidance(self.db)
        seed, special = self.seed_for_episode(n, datetime.now(self.guard.tz).date())
        hit = None
        if self.s.take.rematch and not special and not (seed and seed.scripted):
            hit = rematch_candidate(self.db)
        if hit:
            guidance = f"{rematch_brief(hit)}\n{guidance}".strip()
        plan = self.planner.plan(n, self.db.past_titles(), guidance=guidance, seed=seed)
        job_id = self.db.create_job(n, plan)
        if special:
            specials = json.loads(self.db.kv_get("specials") or "{}")
            specials[str(job_id)] = special
            self.db.kv_set("specials", json.dumps(specials))
            self.notifier.send_message(
                f"📅 #{job_id} is today's special: {seed.emoji} {seed.category}".strip()
            )
        if hit:
            self.db.kv_set(f"rematch:{hit.job_id}", str(job_id))
            self.db.kv_set(f"rematch_of:{job_id}", str(hit.job_id))
            self.notifier.send_message(f"🔁 #{job_id} is the rematch of a hit: {hit.line()}")
        model = self.active_take_model() if self.s.take.enabled else None
        if model:
            self.db.kv_set(f"take_model:{job_id}", model)
        self.db.update_job(job_id, caption=self.caption(plan, n, self.model_label(model)))
        log.info("job %d planned: episode %d %s", job_id, n, plan.title)
        return job_id

    # ---- v3 model choice: per job > bake-off winner > series default
    def active_take_model(self) -> str:
        chosen = self.db.kv_get("take_model")
        return chosen if chosen in self.s.models.takes else self.s.take.model

    def take_model_for(self, job_id: int) -> str:
        return self.db.kv_get(f"take_model:{job_id}") or self.active_take_model()

    def model_label(self, model: str | None) -> str:
        if not model:
            return ""
        return self.s.models.takes[model].label or model

    def caption(self, plan: EpisodePlan, episode_no: int, model_label: str = "") -> str:
        meta, cap = self.s.series, self.s.caption
        header = meta.header_template.format(
            series=meta.name,
            n=episode_no,
            total=meta.total_episodes,
            emoji=plan.emoji,
            category=plan.category,
            title=plan.title,
        ).strip()
        tags = cap.hashtags[:IG_HASHTAG_MAX]
        text = cap.template.format(
            header=header,
            hook=plan.hook.strip(),
            credits=cap.credits.strip().replace("{model}", model_label or "Kling"),
            hashtags=" ".join(tags),
            title=plan.title,
            logline=plan.logline,
        )
        lines = [ln.rstrip() for ln in text.strip().splitlines()]
        out, blank = [], False  # collapse empty sections
        for ln in lines:
            if not ln:
                if not blank:
                    out.append(ln)
                blank = True
            else:
                out.append(ln)
                blank = False
        return "\n".join(out)[:IG_CAPTION_MAX]

    def storyboard_text(self, plan: EpisodePlan) -> str:
        rows = [f"📍 {plan.location.name}"]
        if self.s.take.enabled:
            if plan.motif:
                rows.append(f"🔴 motif: {plan.motif}")
            if plan.direction:
                rows.append(f"➡️ {plan.direction}")
        for i, s in enumerate(plan.shots, 1):
            who = ", ".join(s.cast) or "—"
            secs = f" {s.seconds}s" if self.s.take.enabled else ""
            rows.append(f"{i}. {s.beat.upper()}{secs} · {s.size} {s.angle} · {s.camera} · {who}: {s.action}")
        return "\n".join(rows)[:900]

    # ================================================================== generation
    def _dir(self, job_id: int) -> Path:
        d = self.jobs_dir / str(job_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _plate(self, job_id: int, plan: EpisodePlan) -> str:
        """One location plate per episode, shared by every shot (keeps the place consistent)."""
        if url := self.db.kv_get(f"plate:{job_id}"):
            return url
        world = f" {clip_text(plan.world, 1200)}" if plan.world else ""
        start = (
            f" Props exactly as the story starts: {clip_text(plan.start_state, 400)}"
            if plan.start_state
            else ""
        )
        prompt = (
            f"{plan.location.plate_prompt}.{world}{start} Empty environment, no people, no animals, "
            f"no readable text or signage; posters and screens show plain abstract shapes. {self.s.style.bible}"
        )
        url = self.media.image(
            {
                "prompt": prompt,
                "aspect_ratio": "9:16",
                "image_urls": [],
                "negative_prompt": self.s.style.negative or None,
            },
            job_id,
        )
        local = download(url, self._dir(job_id) / "plate.png")
        # provider URLs expire (Higgsfield keeps outputs ~7 days); keep a permanent copy for redos
        _, url = self.storage.put_public(local, f"plates/{job_id}")
        self.db.kv_set(f"plate:{job_id}", url)
        return url

    def _render_shot(self, job_id: int, idx: int, plan: EpisodePlan, plate: str) -> None:
        shot = plan.shots[idx]
        cast = self.cast_urls()
        missing = [n for n in shot.cast if n not in cast]
        if missing:
            raise CastNotReady(f"no approved character sheet for {', '.join(missing)}; run cast first")
        prompt, roles = compose_shot_prompt(self.s, plan, shot, self.s.models.video.ref_token)
        refs = [plate if r == "location" else cast[r] for r in roles]
        d = self.s.shots.duration_s
        vid_url = self.media.video(
            {
                "prompt": prompt,
                "image_urls": refs,
                "image_url": refs[0],
                "duration": d,
                "duration_str": str(d),
                "aspect_ratio": "9:16",
                "negative_prompt": self.s.style.negative or None,
            },
            job_id,
        )
        local = download(vid_url, self._dir(job_id) / f"shot_{idx}.mp4")
        self.db.bump_shot_attempts(job_id, idx)
        self.db.update_shot(job_id, idx, keyframe_url=plate, video_url=vid_url, local_path=str(local))
        self.db.kv_set(f"prompt:{job_id}:{idx}", prompt)

    def _render_music(
        self, job_id: int, plan: EpisodePlan, seconds: float, chunks: list[dict[str, Any]] | None = None
    ) -> Path | None:
        attempt = self.db.kv_get(f"music_attempt:{job_id}") or "0"
        self.db.kv_set(f"music_attempt:{job_id}", str(int(attempt) + 1))
        return self.music.track(
            plan.music_prompt,
            seconds,
            self._dir(job_id) / f"music_{attempt}.mp3",
            job_id,
            seed=job_id * 100 + int(attempt),
            chunks=chunks,
        )

    def _title_png(self, job_id: int, plan: EpisodePlan) -> Path:
        tc = self.s.title_card
        font = self.s.resolve(tc.font) if tc.font else None
        episode_no = self.db.get_job(job_id)["episode_no"]
        subtitle = tc.subtitle.format(handle=self.s.series.handle, n=episode_no, nn=f"{episode_no:02d}")
        return render_title_card(plan.title, subtitle, tc, font, self._dir(job_id) / "title.png")

    def _boss_card(self, job_id: int, plan: EpisodePlan) -> Path:
        """Fighting-game name card for today's boss, in the title-card font and colours."""
        tc, t = self.s.title_card, self.s.take
        font = self.s.resolve(tc.font) if tc.font else None
        spec = tc.model_copy(update={"y_ratio": t.boss_card_y_ratio})
        seed = self._seed(job_id)
        if self.db.kv_get(f"rematch_of:{job_id}"):
            label = t.boss_card_rematch_label
        else:
            label = seed.card_label if seed and seed.card_label else t.boss_card_label
        return render_title_card(
            plan.category.strip().upper(), label, spec, font, self._dir(job_id) / "boss_card.png"
        )

    @staticmethod
    def _climax_index(plan: EpisodePlan) -> int:
        beats = [s.beat for s in plan.shots]
        return beats.index("climax") if "climax" in beats else len(beats) // 2

    def _cover(self, job_id: int, plan: EpisodePlan, title_png: Path) -> Path:
        """Poster cover: a frame from the climax shot (else the middle one) + the title card."""
        idx = self._climax_index(plan)
        if self.s.take.enabled:  # one take: the climax is a time range inside it
            clip = Path(self.db.kv_get(f"take_path:{job_id}") or "")
            s = plan.shots[idx]
            t = shot_starts(plan)[idx] + s.seconds * 0.6
        else:
            clip = Path(self.db.shots(job_id)[idx]["local_path"])
            t = max(0.0, probe_duration(clip) * 0.6)
        return self._cover_from(job_id, clip, t, title_png)

    def _cover_from(self, job_id: int, clip: Path, t: float, title_png: Path) -> Path:
        frame = self._dir(job_id) / "cover_frame.png"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-ss",
                f"{t:.2f}",
                "-i",
                str(clip),
                "-frames:v",
                "1",
                str(frame),
            ],
            check=True,
        )
        return render_cover(frame, title_png, self._dir(job_id) / "cover.jpg")

    def _assemble(self, job_id: int, plan: EpisodePlan, music: Path | None) -> Path:
        d = self._dir(job_id)
        clips = [Path(s["local_path"]) for s in self.db.shots(job_id)]
        durations = [min(probe_duration(c), float(self.s.shots.duration_s)) for c in clips]
        tc = self.s.title_card
        title_png = self._title_png(job_id, plan)
        out = assemble(
            AssemblyPlan(
                clips,
                durations,
                title_png if tc.in_video else None,
                tc,
                music,
                self.s.music.volume_db,
                d / "final.mp4",
            )
        )
        self.db.update_job(job_id, video_path=str(out))
        if tc.cover:
            self.db.kv_set(f"cover:{job_id}", str(self._cover(job_id, plan, title_png)))
        return out

    # ================================================================== v3: one take
    def _keyframe(self, job_id: int, plan: EpisodePlan) -> str:
        """The opening frame, with every character of the episode painted into the location (the
        video starts from it, so nobody looks pasted on)."""
        if url := self.db.kv_get(f"keyframe:{job_id}"):
            return url
        cast_urls = self.cast_urls()
        cast = episode_cast(plan)
        refs = [cast_urls[n] for n in cast] + [self._plate(job_id, plan)]
        url = self.media.image(
            {
                "prompt": compose_keyframe_prompt(self.s, plan, cast),
                "aspect_ratio": "9:16",
                "image_urls": refs,
                "negative_prompt": self.s.style.negative or None,
            },
            job_id,
        )
        local = download(url, self._dir(job_id) / "keyframe.png")
        _, url = self.storage.put_public(local, f"keyframes/{job_id}")
        self.db.kv_set(f"keyframe:{job_id}", url)
        return url

    # ---- the calendar: date-pinned specials first, then the numbered entries. A special doesn't
    # use up a numbered entry: those shift by the number of specials made before them.
    def _live_specials(self) -> dict[int, tuple[str, int]]:
        """job id -> (date, episode number) for specials whose job still counts."""
        out: dict[int, tuple[str, int]] = {}
        for jid, day in json.loads(self.db.kv_get("specials") or "{}").items():
            job = self.db.get_job(int(jid))
            if job and job["status"] not in (JobStatus.REJECTED, JobStatus.FAILED):
                out[int(jid)] = (day, job["episode_no"])
        return out

    def _numbered_seed(self, n: int, skip: int) -> EpisodeSeed | None:
        i = n - 1 - skip
        return self.s.episodes[i] if 0 <= i < len(self.s.episodes) else None

    def seed_for_episode(self, n: int, today: date | None = None) -> tuple[EpisodeSeed | None, str]:
        """(seed, special date or '') for the next episode, numbered n, planned today."""
        live = self._live_specials()
        if today is not None:
            used = {day for day, _ in live.values()}
            for sp in self.s.specials:
                if sp.date == today and sp.date.isoformat() not in used:
                    return sp, sp.date.isoformat()
        return self._numbered_seed(n, sum(1 for _, e in live.values() if e < n)), ""

    def _seed(self, job_id: int) -> EpisodeSeed | None:
        n = self.db.get_job(job_id)["episode_no"]
        day = json.loads(self.db.kv_get("specials") or "{}").get(str(job_id))
        if day:
            return next((s for s in self.s.specials if s.date.isoformat() == day), None)
        live = self._live_specials()
        return self._numbered_seed(n, sum(1 for jid, (_, e) in live.items() if e < n and jid != job_id))

    def _take_with_fallback(self, name: str, ctx: dict[str, Any], job_id: int) -> str:
        """Make the take on `name`; if that fails for any reason but the budget, make it on the
        model's `fallback` (e.g. Seedance 2.0 on OpenRouter -> the same model on Higgsfield)."""
        spec = self.s.models.takes[name]
        try:
            return self.media.take(name, ctx, job_id)
        except BudgetExceeded:
            raise
        except Exception as e:
            fb = spec.fallback
            if not fb or fb not in self.s.models.takes:
                raise
            label = self.s.models.takes[fb].label or fb
            log.warning("take on %s failed for job %d (%s); falling back to %s", name, job_id, e, fb)
            self.notifier.send_message(
                f"⚠️ #{job_id}: {spec.label or name} via {spec.provider} failed ({str(e)[:200]}). "
                f"Making it with {label} via {self.s.models.takes[fb].provider} instead."
            )
            return self.media.take(fb, ctx, job_id)

    def _render_take(self, job_id: int, plan: EpisodePlan) -> Path:
        name = self.take_model_for(job_id)
        spec = self.s.models.takes[name]
        keyframe = self._keyframe(job_id, plan) if spec.start_frame else None
        # the fallback gets the same prompt, so it has to fit both limits
        fb = self.s.models.takes.get(spec.fallback) if spec.fallback else None
        caps = [c for c in (spec.max_prompt_chars, fb.max_prompt_chars if fb else 0) if c]
        tp = compose_take_prompt(self.s, plan, spec.ref_token, spec.start_frame, min(caps, default=0))
        cast_urls = self.cast_urls()
        refs = [
            keyframe if r == "opening" else self._plate(job_id, plan) if r == "location" else cast_urls[r]
            for r in tp.roles
        ]
        prompt = tp.prompt
        video_refs: list[str] = []
        seed = self._seed(job_id)
        if seed and seed.reference_video and "{video_urls}" in json.dumps(spec.input):
            video_refs = [seed.reference_video]
            prompt = (
                "Use video 1 as the motion, timing and camera reference only; keep our characters, "
                "location and style.\n" + prompt
            )
        seconds = sum(s.seconds for s in plan.shots)
        url = self._take_with_fallback(
            name,
            {
                "prompt": prompt,
                "prompt_global": tp.global_prompt,
                "image_urls": refs,
                # the same images as OpenRouter's reference list (image 1 = the opening frame)
                "image_refs": [{"type": "image_url", "image_url": {"url": u}} for u in refs],
                "video_urls": video_refs,
                "start_image": keyframe or refs[0],
                "shots": tp.shots,
                "duration": seconds,
                "duration_str": str(seconds),
                "aspect_ratio": "9:16",
                "negative_prompt": self.s.style.negative or None,
            },
            job_id,
        )
        attempt = int(self.db.kv_get(f"take_attempt:{job_id}") or "0") + 1
        self.db.kv_set(f"take_attempt:{job_id}", str(attempt))
        local = download(url, self._dir(job_id) / f"take_{attempt}.mp4")
        self.db.kv_set(f"take_path:{job_id}", str(local))
        self.db.kv_set(f"prompt:{job_id}:take", prompt)
        return local

    def _take_music(self, job_id: int, plan: EpisodePlan) -> Path | None:
        """Reuse a finished track (resume, or shared across a bake-off); else compose one."""
        prev = self.db.kv_get(f"music_path:{job_id}")
        if prev and Path(prev).exists():
            return Path(prev)
        bpm = self.s.take.music_bpm
        if bpm and "bpm" not in plan.music_prompt.lower():
            plan = plan.model_copy(update={"music_prompt": f"{plan.music_prompt.rstrip('.')}, {bpm} BPM"})
        episode_no = self.db.get_job(job_id)["episode_no"]
        chunks = music_chunks(self.s, plan, triumph=wins_this_episode(self.s, episode_no))
        music = self._render_music(job_id, plan, self.s.take.seconds, chunks)
        self.db.kv_set(f"music_path:{job_id}", str(music) if music else "")
        return music

    def _assemble_take(self, job_id: int, plan: EpisodePlan, music: Path | None) -> Path:
        t = self.s.take
        take = Path(self.db.kv_get(f"take_path:{job_id}") or "")
        starts = shot_starts(plan)
        ci = self._climax_index(plan)
        cuts = detect_cuts(take) if (t.flash_on_climax or t.impact_frame or t.quiet_before_twist_s) else []
        climax_at = snap(float(starts[ci]), cuts)  # the model picks the exact cut frame
        dips = []
        if t.climax_dip_s > 0 and climax_at > t.climax_dip_s:  # tension: music drops, then slams back
            dips.append((climax_at - t.climax_dip_s, climax_at))
        beats = [s.beat for s in plan.shots]
        if t.quiet_before_twist_s > 0 and "twist" in beats:  # one quiet beat after the burst
            twist_at = snap(float(starts[beats.index("twist")]), cuts)
            dips.append((twist_at, twist_at + t.quiet_before_twist_s))
        flashes = [climax_at] if t.flash_on_climax and climax_at > 0 else []
        impacts, speed_lines = [], None
        if t.impact_frame and climax_at > 0:  # right after the flash: the anime impact frame
            a = climax_at + (2 / FPS if flashes else 0)
            impacts = [(a, a + IMPACT_FRAMES / FPS)]
            speed_lines = render_speed_lines(self._dir(job_id) / "speed_lines.png")
        # shots with a spoken line: the model's own audio comes up, the music ducks, subtitles show
        # the music ducks only while the words are spoken (not the whole shot), so a short punchline
        # like "...How?" doesn't leave the payoff in near-silence
        speech = [
            (float(t0), float(min(t0 + s.seconds, t0 + spoken_seconds(s.line))))
            for s, t0 in zip(plan.shots, starts, strict=True)
            if s.line
        ]
        subs = []
        if t.subtitles:
            font = (
                self.s.resolve(self.s.title_card.subtitle_font) if self.s.title_card.subtitle_font else None
            )
            for k, (s, t0) in enumerate(zip(plan.shots, starts, strict=True)):
                if s.line:
                    png = render_subtitle(
                        s.line,
                        font,
                        self._dir(job_id) / f"sub_{k}.png",
                        color=self.s.title_card.subtitle_color,
                    )
                    subs.append((png, t0 + 0.15, t0 + s.seconds - 0.1))
        if t.boss_card_s > 0 and plan.category.strip():  # "SNOOZE BUTTON / FINAL BOSS"
            a = t.boss_card_from_s
            subs.append((self._boss_card(job_id, plan), a, a + t.boss_card_s))
        out = assemble_take(
            TakeAssembly(
                take=take,
                seconds=float(sum(s.seconds for s in plan.shots)),
                music=music,
                music_volume_db=t.music_volume_db,
                sfx_volume_db=t.sfx_volume_db,
                dips=dips,
                out=self._dir(job_id) / "final.mp4",
                speech=speech,
                subtitles=subs,
                flashes=flashes,
                impacts=impacts,
                speed_lines=speed_lines,
            )
        )
        self.db.update_job(job_id, video_path=str(out))
        if self.s.title_card.cover:
            self.db.kv_set(f"cover:{job_id}", str(self._cover(job_id, plan, self._title_png(job_id, plan))))
        return out

    def _generate_take(self, job_id: int, plan: EpisodePlan) -> Path:
        prev = self.db.kv_get(f"take_path:{job_id}")
        if not (prev and Path(prev).exists()):  # resume: a finished take is already paid for
            self._render_take(job_id, plan)
        return self._assemble_take(job_id, plan, self._take_music(job_id, plan))

    def _reassemble(self, job_id: int, plan: EpisodePlan, music: Path | None) -> Path:
        if self.s.take.enabled:
            return self._assemble_take(job_id, plan, music)
        return self._assemble(job_id, plan, music)

    def generate(self, job_id: int) -> Path:
        plan = self.db.plan(job_id)
        self.db.update_job(job_id, status=JobStatus.GENERATING, error=None)
        try:
            missing = [n for n in {n for s in plan.shots for n in s.cast} if n not in self.cast_urls()]
            if missing:
                raise CastNotReady(
                    f"no approved character sheet for {', '.join(sorted(missing))}; run cast first"
                )
            if self.s.take.enabled:
                plan = self._directed(plan, job_id)
                out = self._generate_take(job_id, plan)
            else:
                plate = self._plate(job_id, plan)
                for idx, row in enumerate(self.db.shots(job_id)):
                    if row["local_path"] and Path(row["local_path"]).exists():
                        continue  # resume: shot already paid for
                    self._render_shot(job_id, idx, plan, plate)
                music = self._render_music(job_id, plan, self.s.shots.count * self.s.shots.duration_s)
                self.db.kv_set(f"music_path:{job_id}", str(music) if music else "")
                out = self._assemble(job_id, plan, music)
        except BudgetExceeded as e:
            self.db.update_job(job_id, status=JobStatus.FAILED, error=str(e))
            self.notifier.send_message(f"⛔ Job {job_id} stopped by budget guard: {e}")
            raise
        except Exception as e:
            self.db.update_job(job_id, status=JobStatus.FAILED, error=f"{type(e).__name__}: {e}"[:1000])
            self.notifier.send_message(f"⚠️ Job {job_id} failed: {type(e).__name__}: {e}"[:1000])
            raise
        self.send_for_review(job_id)
        return out

    # ================================================================== review
    def send_for_review(self, job_id: int) -> None:
        job = self.db.get_job(job_id)
        plan = self.db.plan(job_id)
        spent = self.db.spend_for_job(job_id)
        month = self.guard.month_spend()
        if cover := self.db.kv_get(f"cover:{job_id}"):
            self.notifier.send_photo(
                Path(cover), f"Cover · #{job_id} {plan.title}\n\n{self.storyboard_text(plan)}"
            )
        music_regen = not isinstance(self.music, NoMusic)
        model = ""
        if self.s.take.enabled:
            model = self.model_label(self.take_model_for(job_id))
            in_bakeoff = len(json.loads(self.db.kv_get(f"bakeoff:{job_id}") or "[]")) > 1
            markup = take_keyboard(job_id, music_regen, model if in_bakeoff else None)
        else:
            markup = review_keyboard(job_id, len(plan.shots), music_regen)
        meta = (
            f"#{job_id} · Ep {job['episode_no']} · {plan.title}" + (f" · {model}" if model else "") + "\n"
            f"cost ${spent:.2f} · month ${month:.2f}/{self.s.budget.monthly_usd:.0f}\n"
            f"Reply to this video with new text to replace the hook.\n\n"
        )
        msg_id = self.notifier.send_video(Path(job["video_path"]), meta + job["caption"], markup)
        self.db.update_job(job_id, status=JobStatus.AWAITING_REVIEW, tg_message_id=msg_id)

    def _require(self, job_id: int, *allowed: JobStatus) -> dict[str, Any]:
        job = self.db.get_job(job_id)
        if not job:
            raise KeyError(f"job {job_id} not found")
        if job["status"] not in allowed:
            raise ValueError(f"job {job_id} is {job['status']}, expected one of {[a.value for a in allowed]}")
        return job

    def approve(self, job_id: int, publish_after: datetime | None = None, note: str = "") -> None:
        self._require(job_id, JobStatus.AWAITING_REVIEW)
        base = (publish_after or datetime.now(UTC)).astimezone(UTC)
        when = base.isoformat(timespec="seconds")
        self.db.update_job(job_id, status=JobStatus.APPROVED, publish_after=when)
        self.db.kv_set(f"slots:{job_id}", json.dumps(self.release_slots(base)))
        self.db.add_feedback(job_id, "approved", note)

    # ---- when each platform gets the reel (schedule.stagger / rotate)
    def release_slots(self, base: datetime) -> dict[str, str]:
        enabled = ["instagram", *self.crosspost_platforms()]
        sched = self.s.schedule
        order = [p for p in sched.stagger if p in enabled]
        if sched.rotate and len(order) > 1:
            k = base.isocalendar().week % len(order)
            order = order[k:] + order[:k]
        offsets = {p: i for i, p in enumerate(order)}
        return {
            p: (base + timedelta(days=offsets.get(p, 0))).astimezone(UTC).isoformat(timespec="seconds")
            for p in enabled
        }

    def slots(self, job_id: int) -> dict[str, str] | None:
        """Per-platform release times, or None for reels approved before staggering existed.
        With no stagger, every platform goes at once: a reel approved while staggering was on is
        moved to its earliest slot, so nothing waits days for a schedule that no longer applies."""
        raw = self.db.kv_get(f"slots:{job_id}")
        if not raw:
            return None
        slots: dict[str, str] = json.loads(raw)
        if not self.s.schedule.stagger and len(set(slots.values())) > 1:
            first = min(slots.values())
            slots = dict.fromkeys(slots, first)
            self.db.kv_set(f"slots:{job_id}", json.dumps(slots))
        return slots

    def release_plan_text(self, job_id: int, tz: Any) -> str:
        slots = self.slots(job_id) or {}
        names = {"instagram": "Instagram", **CROSSPOST_NAMES}
        parts = sorted(slots.items(), key=lambda kv: kv[1])
        return " · ".join(f"{names[p]} {datetime.fromisoformat(t).astimezone(tz):%a %H:%M}" for p, t in parts)

    def reject(self, job_id: int, note: str = "") -> None:
        job = self._require(job_id, JobStatus.AWAITING_REVIEW, JobStatus.APPROVED, JobStatus.FAILED)
        self.db.update_job(job_id, status=JobStatus.REJECTED)
        if job["status"] != JobStatus.FAILED or note:  # a dead render says nothing about the story
            self.db.add_feedback(job_id, "rejected", note)

    def add_note(self, job_id: int | None, text: str) -> None:
        """Your words about an episode (or the series, with no job): the strongest evidence the
        lessons are built from."""
        text = text.strip()
        if not text:
            raise ValueError("the note is empty")
        if job_id is not None and not self.db.get_job(job_id):
            raise KeyError(f"job {job_id} not found")
        self.db.add_feedback(job_id, "note", text)

    def retake(self, job_id: int, note: str = "") -> Path:
        """v3: render the whole take again (same storyboard, opening frame and music)."""
        self._require(job_id, JobStatus.AWAITING_REVIEW)
        if not self.s.take.enabled:
            raise ValueError("retake is for one-take reels; use the shot buttons")
        self.db.add_feedback(job_id, "retake", note)
        plan = self._directed(self.db.plan(job_id), job_id)  # older plans get the full write-up too
        self.db.update_job(job_id, status=JobStatus.GENERATING)
        try:
            self._render_take(job_id, plan)
            music = self.db.kv_get(f"music_path:{job_id}") or None
            self._assemble_take(job_id, plan, Path(music) if music else None)
        except Exception as e:
            self.db.update_job(job_id, status=JobStatus.AWAITING_REVIEW)
            self.notifier.send_message(f"⚠️ Retake of #{job_id} failed: {e}"[:1000])
            raise
        self.send_for_review(job_id)
        return Path(self.db.get_job(job_id)["video_path"])

    def bakeoff(self, models: list[str] | None = None) -> list[int]:
        """Plan ONE episode and render it on several video models. Storyboard, location plate,
        opening frame and music are shared, so the only difference is the model."""
        if not self.s.take.enabled:
            raise ValueError("bake-off needs take.enabled in the series file")
        names = list(dict.fromkeys(models or self.s.take.bakeoff or [self.active_take_model()]))
        unknown = [n for n in names if n not in self.s.models.takes]
        if unknown:
            raise ValueError(f"unknown model(s) {unknown}; choose from {sorted(self.s.models.takes)}")
        n = self.db.next_episode_no()
        plan = self.planner.plan(n, self.db.past_titles(), guidance=planner_guidance(self.db))
        plan = self._directed(plan)  # once: every model gets the same written-out shots
        ids = []
        for name in names:
            jid = self.db.create_job(n, plan)
            self.db.kv_set(f"take_model:{jid}", name)
            self.db.update_job(jid, caption=self.caption(plan, n, self.model_label(name)))
            ids.append(jid)
        for jid in ids:
            self.db.kv_set(f"bakeoff:{jid}", json.dumps(ids))
        self.notifier.send_message(
            f"🥊 Bake-off for episode {n} ({plan.title}): "
            + " vs ".join(self.model_label(m) for m in names)
            + ". Same storyboard, opening frame and music; previews arrive one by one."
        )
        for i, jid in enumerate(ids):
            if i:
                self._share_from(ids[0], jid)
            try:
                self.generate(jid)
            except Exception:  # generate() already reported it; the other models still run
                log.exception("bake-off job %d (%s) failed", jid, names[i])
        return ids

    def _share_from(self, src: int, dst: int) -> None:
        for key in ("plate", "keyframe"):
            if v := self.db.kv_get(f"{key}:{src}"):
                self.db.kv_set(f"{key}:{dst}", v)
        music = self.db.kv_get(f"music_path:{src}")
        if music and Path(music).exists():
            copy = self._dir(dst) / Path(music).name
            shutil.copyfile(music, copy)
            self.db.kv_set(f"music_path:{dst}", str(copy))

    def pick_winner(self, job_id: int) -> tuple[str, list[int]]:
        """Make this job's model the default for every reel; reject its bake-off siblings."""
        self._require(job_id, JobStatus.AWAITING_REVIEW, JobStatus.APPROVED)
        name = self.take_model_for(job_id)
        self.db.kv_set("take_model", name)
        rejected = []
        for sib in json.loads(self.db.kv_get(f"bakeoff:{job_id}") or "[]"):
            job = self.db.get_job(sib)
            if sib != job_id and job and job["status"] in (JobStatus.AWAITING_REVIEW, JobStatus.FAILED):
                self.db.update_job(sib, status=JobStatus.REJECTED)
                rejected.append(sib)
        return name, rejected

    def regenerate_shot(self, job_id: int, idx: int) -> Path:
        self._require(job_id, JobStatus.AWAITING_REVIEW)
        if self.s.take.enabled:
            raise ValueError("this reel is one take: use 🔁 Retake")
        plan = self.db.plan(job_id)
        if not 0 <= idx < len(plan.shots):
            raise ValueError(f"shot index {idx} out of range")
        self.db.update_job(job_id, status=JobStatus.GENERATING)
        try:
            self._render_shot(job_id, idx, plan, self._plate(job_id, plan))
            music = self.db.kv_get(f"music_path:{job_id}") or None
            self._assemble(job_id, plan, Path(music) if music else None)
        except Exception as e:
            self.db.update_job(job_id, status=JobStatus.AWAITING_REVIEW)  # previous cut is still valid
            self.notifier.send_message(f"⚠️ Regenerating shot {idx + 1} of #{job_id} failed: {e}"[:1000])
            raise
        self.send_for_review(job_id)
        return Path(self.db.get_job(job_id)["video_path"])

    def regenerate_music(self, job_id: int) -> Path:
        self._require(job_id, JobStatus.AWAITING_REVIEW)
        plan = self.db.plan(job_id)
        self.db.update_job(job_id, status=JobStatus.GENERATING)
        try:
            if self.s.take.enabled:
                self.db.kv_set(f"music_path:{job_id}", "")  # force a fresh track
                music = self._take_music(job_id, plan)
            else:
                music = self._render_music(job_id, plan, self.s.shots.count * self.s.shots.duration_s)
                self.db.kv_set(f"music_path:{job_id}", str(music) if music else "")
            self._reassemble(job_id, plan, music)
        except Exception as e:
            self.db.update_job(job_id, status=JobStatus.AWAITING_REVIEW)
            self.notifier.send_message(f"⚠️ New music for #{job_id} failed: {e}"[:1000])
            raise
        self.send_for_review(job_id)
        return Path(self.db.get_job(job_id)["video_path"])

    def set_hook(self, job_id: int, hook: str) -> str:
        job = self._require(job_id, JobStatus.AWAITING_REVIEW, JobStatus.APPROVED)
        plan = self.db.plan(job_id)
        plan.hook = hook.strip()
        model = self.model_label(self.take_model_for(job_id)) if self.s.take.enabled else ""
        caption = self.caption(plan, job["episode_no"], model)
        self.db.update_job(job_id, plan_json=plan.model_dump_json(), caption=caption)
        return caption

    # ================================================================== publishing
    def publish(self, job_id: int) -> str:
        job = self._require(job_id, JobStatus.APPROVED)
        self.db.update_job(job_id, status=JobStatus.PUBLISHING)
        try:
            self.publisher.refresh_token_if_due()
            _, url = self.storage.put_public(Path(job["video_path"]), f"reels/{job_id}")
            self.db.update_job(job_id, public_url=url)
            cover_url = None
            if (cover := self.db.kv_get(f"cover:{job_id}")) and Path(cover).exists():
                _, cover_url = self.storage.put_public(Path(cover), f"reels/{job_id}")
            media_id = self.publisher.publish_reel(
                url,
                limit_hashtags(self.with_affiliate(job["caption"], "instagram")),
                self.s.title_card.cover_offset_ms,
                cover_url=cover_url,
            )
        except Exception as e:
            self.db.update_job(
                job_id, status=JobStatus.FAILED, error=f"publish: {type(e).__name__}: {e}"[:1000]
            )
            self.notifier.send_message(f"⚠️ Publishing #{job_id} failed: {e}"[:1000])
            raise
        self.db.update_job(job_id, status=JobStatus.PUBLISHED, ig_media_id=media_id)
        link = self.publisher.permalink(media_id) or media_id
        self.notifier.send_message(f"🚀 #{job_id} is live: {link}")
        self.cross_post(job_id, after_publish=True)
        return media_id

    # ---- cross-posting: the same reel on YouTube Shorts / Facebook right after Instagram. A failure
    # never touches the Instagram post; later runs retry each platform up to CROSSPOST_TRIES times.
    def crosspost_platforms(self) -> dict[str, Any]:
        pairs = (("youtube", self.youtube), ("facebook", self.facebook))
        return {name: client for name, client in pairs if client is not None}

    def _meta_values(self, job_id: int) -> dict[str, Any]:
        job = self.db.get_job(job_id)
        plan = self.db.plan(job_id)
        cap = self.s.caption
        model = self.model_label(self.take_model_for(job_id)) if self.s.take.enabled else ""
        return {
            "hook_line": plan.hook.strip().splitlines()[0].strip() if plan.hook.strip() else plan.title,
            "hook": plan.hook.strip(),
            "title": plan.title,
            "emoji": plan.emoji,
            "category": plan.category,
            "n": job["episode_no"],
            "series": self.s.series.name,
            "credits": cap.credits.strip().replace("{model}", model or "Kling"),
            "hashtags": " ".join(cap.hashtags[:3]),  # YouTube shows the first 3 above the title
        }

    def with_affiliate(self, text: str, platform: str) -> str:
        """Add the affiliate lines just above the hashtags (each labelled as an affiliate link)."""
        add = [a for a in self.s.caption.affiliate_lines(platform) if a and a not in text]
        if not add:
            return text
        block = "\n".join(add)
        lines = text.rstrip().splitlines()
        if lines and lines[-1].lstrip().startswith("#"):
            return "\n".join(lines[:-1]).rstrip() + f"\n\n{block}\n\n" + lines[-1]
        return text.rstrip() + f"\n\n{block}"

    def youtube_meta(self, job_id: int) -> tuple[str, str, list[str]]:
        values, yt = self._meta_values(job_id), self.s.youtube
        title = " ".join(yt.title.format(**values).split())
        description = self.with_affiliate(yt.description.format(**values).strip(), "youtube")
        return title, description, [*self.s.caption.hashtags, self.s.series.name]

    def _video_file(self, job_id: int) -> Path:
        job = self.db.get_job(job_id)
        if not job:
            raise KeyError(f"job {job_id} not found")
        path = Path(job["video_path"] or "")
        if path.exists():
            return path
        if not job["public_url"]:  # a later run: fetch the public copy Instagram was given
            raise ValueError(f"job {job_id} has no video to upload")
        dest = self._dir(job_id) / "crosspost.mp4"
        return dest if dest.exists() else download(job["public_url"], dest)

    def post_to(self, platform: str, job_id: int) -> str:
        client = self.crosspost_platforms().get(platform)
        if client is None:
            raise ValueError(CROSSPOST_OFF.get(platform, f"unknown platform {platform!r}"))
        if done := self.db.kv_get(f"{platform}_id:{job_id}"):
            return done
        path = self._video_file(job_id)
        if platform == "youtube":
            title, description, tags = self.youtube_meta(job_id)
            yt = self.s.youtube
            post_id = client.upload_short(
                path,
                title,
                description,
                tags,
                category_id=yt.category_id,
                made_for_kids=yt.made_for_kids,
                synthetic_media=yt.synthetic_media,
            )
            link = f"▶️ #{job_id} is on YouTube Shorts: {shorts_url(post_id)}"
        else:
            fb = self.s.facebook
            title = " ".join(fb.title.format(**self._meta_values(job_id)).split())
            post_id = client.publish_reel(
                path,
                self.with_affiliate(self.db.get_job(job_id)["caption"] or title, "facebook"),
                title=title,
                ai_generated=fb.ai_label,
            )
            link = f"📘 #{job_id} is on Facebook: {reel_url(post_id)}"
        self.db.kv_set(f"{platform}_id:{job_id}", post_id)
        self.notifier.send_message(link)
        return post_id

    def due_platforms(self, job_id: int, now: datetime, after_publish: bool = False) -> list[str]:
        slots, now_iso = self.slots(job_id), now.astimezone(UTC).isoformat(timespec="seconds")
        out = []
        for p in self.crosspost_platforms():
            tries = int(self.db.kv_get(f"{p}_tries:{job_id}") or 0)
            if self.db.kv_get(f"{p}_id:{job_id}") or tries >= CROSSPOST_TRIES:
                continue
            # approved before staggering existed: right after Instagram, then only retries (so a reel
            # from before a platform was set up is never back-posted on its own)
            due = slots.get(p, slots.get("instagram", "")) <= now_iso if slots else after_publish or tries > 0
            if due:
                out.append(p)
        return out

    def cross_post(
        self, job_id: int, now: datetime | None = None, after_publish: bool = False
    ) -> dict[str, str]:
        out = {}
        for platform in self.due_platforms(job_id, now or datetime.now(UTC), after_publish):
            tries = int(self.db.kv_get(f"{platform}_tries:{job_id}") or 0) + 1
            self.db.kv_set(f"{platform}_tries:{job_id}", str(tries))
            try:
                out[platform] = self.post_to(platform, job_id)
            except Exception as e:
                log.exception("%s upload of #%s failed", platform, job_id)
                more = " Trying again on the next runs." if tries < CROSSPOST_TRIES else ""
                name = CROSSPOST_NAMES[platform]
                self.notifier.send_message(f"⚠️ {name} upload of #{job_id} failed: {e}"[:900] + more)
        return out

    def publish_due(self, now: datetime | None = None) -> list[str]:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        now_iso = now.isoformat(timespec="seconds")
        published = []
        for job in self.db.jobs_with_status(JobStatus.APPROVED):
            slots = self.slots(job["id"])
            ig_at = (
                slots.get("instagram", job["publish_after"] or "") if slots else job["publish_after"] or ""
            )
            if ig_at <= now_iso:
                try:
                    published.append(self.publish(job["id"]))
                except Exception:
                    log.exception("publish failed for job %s", job["id"])
            elif slots:  # a platform whose day comes before Instagram's this week
                self.cross_post(job["id"], now)
        for job in self.db.jobs_with_status(JobStatus.PUBLISHED):  # later days + earlier failures
            if self.due_platforms(job["id"], now):
                self.cross_post(job["id"], now)
        return published
