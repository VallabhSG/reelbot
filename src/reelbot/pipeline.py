"""Orchestration: plan -> keyframes -> shots -> music -> assemble -> review -> publish."""

from __future__ import annotations

import logging
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
from .telegram import review_keyboard
from .titlecard import render_title_card

log = logging.getLogger(__name__)

IG_CAPTION_MAX = 2200
IG_HASHTAG_MAX = 30


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

    # ------------------------------------------------------------------ planning
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

    # ------------------------------------------------------------------ generation
    def _dir(self, job_id: int) -> Path:
        d = self.jobs_dir / str(job_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _ctx_base(self) -> dict[str, Any]:
        d = self.s.shots.duration_s
        return {
            "negative_prompt": self.s.style.negative or None,
            "aspect_ratio": "9:16",
            "duration": d,
            "duration_str": str(d),
        }

    def _render_shot(self, job_id: int, idx: int, plan: EpisodePlan, refs: list[str]) -> None:
        shot = plan.shots[idx]
        d = self._dir(job_id)
        base = self._ctx_base()
        kf_url = self.media.image(
            {
                **base,
                "prompt": f"{shot.keyframe_prompt}. {self.s.style.bible}".strip(),
                "reference_urls": refs,
                "image_urls": refs,
            },
            job_id,
        )
        download(kf_url, d / f"kf_{idx}.png")
        vid_url = self.media.video(
            {
                **base,
                "prompt": f"{shot.motion_prompt}. {self.s.style.motion_suffix}".strip(" ."),
                "image_url": kf_url,
                "image_urls": [kf_url, *refs],
                "reference_urls": refs,
            },
            job_id,
        )
        local = download(vid_url, d / f"shot_{idx}.mp4")
        self.db.bump_shot_attempts(job_id, idx)
        self.db.update_shot(job_id, idx, keyframe_url=kf_url, video_url=vid_url, local_path=str(local))

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

    def _assemble(self, job_id: int, plan: EpisodePlan, music: Path | None) -> Path:
        d = self._dir(job_id)
        shots = self.db.shots(job_id)
        clips = [Path(s["local_path"]) for s in shots]
        durations = [min(probe_duration(c), float(self.s.shots.duration_s)) for c in clips]
        tc = self.s.title_card
        font = self.s.resolve(tc.font) if tc.font else None
        title_png = render_title_card(
            plan.title, tc.subtitle.format(handle=self.s.series.handle), tc, font, d / "title.png"
        )
        out = assemble(
            AssemblyPlan(clips, durations, title_png, tc, music, self.s.music.volume_db, d / "final.mp4")
        )
        self.db.update_job(job_id, video_path=str(out))
        return out

    def generate(self, job_id: int) -> Path:
        plan = self.db.plan(job_id)
        self.db.update_job(job_id, status=JobStatus.GENERATING, error=None)
        try:
            refs = self.media.reference_urls(self.s.characters)
            for idx, row in enumerate(self.db.shots(job_id)):
                if row["local_path"] and Path(row["local_path"]).exists():
                    continue  # resume: shot already paid for
                self._render_shot(job_id, idx, plan, refs)
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

    # ------------------------------------------------------------------ review
    def send_for_review(self, job_id: int) -> None:
        job = self.db.get_job(job_id)
        plan = self.db.plan(job_id)
        spent = self.db.spend_for_job(job_id)
        month = self.guard.month_spend()
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
            self._render_shot(job_id, idx, plan, self.media.reference_urls(self.s.characters))
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

    # ------------------------------------------------------------------ publishing
    def publish(self, job_id: int) -> str:
        job = self._require(job_id, JobStatus.APPROVED)
        self.db.update_job(job_id, status=JobStatus.PUBLISHING)
        try:
            self.publisher.refresh_token_if_due()
            key, url = self.storage.put_public(Path(job["video_path"]), f"reels/{job_id}")
            self.db.update_job(job_id, public_url=url)
            media_id = self.publisher.publish_reel(url, job["caption"], self.s.title_card.cover_offset_ms)
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
