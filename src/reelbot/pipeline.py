"""Orchestration (v2):

cast (once):  character sheets -> Telegram -> you pick one per character -> reused forever
per episode:  storyboard (LLM) -> location plate (image) -> N shots (reference-to-video, every shot
              gets the cast sheets + the plate as @image refs) -> music -> edit -> poster cover
              -> Telegram review -> Instagram (with the poster as the Reels cover)
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .assemble import AssemblyPlan, assemble, probe_duration
from .budget import BudgetExceeded, BudgetGuard
from .db import DB
from .models import EpisodePlan, JobStatus
from .planner import Planner
from .providers import MediaProvider
from .providers._http import download
from .providers.music import MusicProvider, NoMusic
from .series import Series
from .storage import Storage
from .storyboard import compose_shot_prompt
from .telegram import cast_keyboard, cast_more_keyboard, review_keyboard
from .titlecard import render_cover, render_title_card

log = logging.getLogger(__name__)

IG_CAPTION_MAX = 2200
IG_HASHTAG_MAX = 30


class CastNotReady(RuntimeError):
    pass


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
        plan = self.planner.plan(n, self.db.past_titles())
        job_id = self.db.create_job(n, plan)
        self.db.update_job(job_id, caption=self.caption(plan, n))
        log.info("job %d planned: episode %d %s", job_id, n, plan.title)
        return job_id

    def caption(self, plan: EpisodePlan, episode_no: int) -> str:
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
            credits=cap.credits.strip(),
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
        for i, s in enumerate(plan.shots, 1):
            who = ", ".join(s.cast) or "—"
            rows.append(f"{i}. {s.beat.upper()} · {s.size} {s.angle} · {s.camera} · {who}: {s.action}")
        return "\n".join(rows)

    # ================================================================== generation
    def _dir(self, job_id: int) -> Path:
        d = self.jobs_dir / str(job_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _plate(self, job_id: int, plan: EpisodePlan) -> str:
        """One location plate per episode, shared by every shot (keeps the place consistent)."""
        if url := self.db.kv_get(f"plate:{job_id}"):
            return url
        prompt = (
            f"{plan.location.plate_prompt}. Empty environment, no people, no animals, "
            f"no readable text or signage. {self.s.style.bible}"
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

    def _render_music(self, job_id: int, plan: EpisodePlan, seconds: float) -> Path | None:
        attempt = self.db.kv_get(f"music_attempt:{job_id}") or "0"
        self.db.kv_set(f"music_attempt:{job_id}", str(int(attempt) + 1))
        return self.music.track(
            plan.music_prompt,
            seconds,
            self._dir(job_id) / f"music_{attempt}.mp3",
            job_id,
            seed=job_id * 100 + int(attempt),
        )

    def _title_png(self, job_id: int, plan: EpisodePlan) -> Path:
        tc = self.s.title_card
        font = self.s.resolve(tc.font) if tc.font else None
        episode_no = self.db.get_job(job_id)["episode_no"]
        subtitle = tc.subtitle.format(handle=self.s.series.handle, n=episode_no, nn=f"{episode_no:02d}")
        return render_title_card(plan.title, subtitle, tc, font, self._dir(job_id) / "title.png")

    def _cover(self, job_id: int, plan: EpisodePlan, title_png: Path) -> Path:
        """Poster cover: a frame from the climax shot (else the middle one) + the title card."""
        beats = [s.beat for s in plan.shots]
        idx = beats.index("climax") if "climax" in beats else len(beats) // 2
        clip = Path(self.db.shots(job_id)[idx]["local_path"])
        frame = self._dir(job_id) / "cover_frame.png"
        t = max(0.0, probe_duration(clip) * 0.6)
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

    def generate(self, job_id: int) -> Path:
        plan = self.db.plan(job_id)
        self.db.update_job(job_id, status=JobStatus.GENERATING, error=None)
        try:
            missing = [n for n in {n for s in plan.shots for n in s.cast} if n not in self.cast_urls()]
            if missing:
                raise CastNotReady(
                    f"no approved character sheet for {', '.join(sorted(missing))}; run cast first"
                )
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
        meta = (
            f"#{job_id} · Ep {job['episode_no']} · {plan.title}\n"
            f"cost ${spent:.2f} · month ${month:.2f}/{self.s.budget.monthly_usd:.0f}\n"
            f"Reply to this video with new text to replace the hook.\n\n"
        )
        markup = review_keyboard(job_id, len(plan.shots), not isinstance(self.music, NoMusic))
        msg_id = self.notifier.send_video(Path(job["video_path"]), meta + job["caption"], markup)
        self.db.update_job(job_id, status=JobStatus.AWAITING_REVIEW, tg_message_id=msg_id)

    def _require(self, job_id: int, *allowed: JobStatus) -> dict[str, Any]:
        job = self.db.get_job(job_id)
        if not job:
            raise KeyError(f"job {job_id} not found")
        if job["status"] not in allowed:
            raise ValueError(f"job {job_id} is {job['status']}, expected one of {[a.value for a in allowed]}")
        return job

    def approve(self, job_id: int, publish_after: datetime | None = None) -> None:
        self._require(job_id, JobStatus.AWAITING_REVIEW)
        when = (publish_after or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")
        self.db.update_job(job_id, status=JobStatus.APPROVED, publish_after=when)

    def reject(self, job_id: int) -> None:
        self._require(job_id, JobStatus.AWAITING_REVIEW, JobStatus.APPROVED, JobStatus.FAILED)
        self.db.update_job(job_id, status=JobStatus.REJECTED)

    def regenerate_shot(self, job_id: int, idx: int) -> Path:
        self._require(job_id, JobStatus.AWAITING_REVIEW)
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
            music = self._render_music(job_id, plan, self.s.shots.count * self.s.shots.duration_s)
            self.db.kv_set(f"music_path:{job_id}", str(music) if music else "")
            self._assemble(job_id, plan, music)
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
        caption = self.caption(plan, job["episode_no"])
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
                url, job["caption"], self.s.title_card.cover_offset_ms, cover_url=cover_url
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
        return media_id

    def publish_due(self, now: datetime | None = None) -> list[str]:
        now_iso = (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")
        published = []
        for job in self.db.jobs_with_status(JobStatus.APPROVED):
            if (job["publish_after"] or "") <= now_iso:
                try:
                    published.append(self.publish(job["id"]))
                except Exception:
                    log.exception("publish failed for job %s", job["id"])
        return published
