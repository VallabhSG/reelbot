"""Long-running worker: daily generation on schedule, Telegram review loop, timed publishing.

Single-threaded on purpose: Telegram queues updates server-side, so long generations simply delay
button handling instead of racing it. State lives in SQLite, so restarts resume cleanly.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .budget import BudgetExceeded
from .db import DB
from .models import JobStatus
from .pipeline import Pipeline
from .series import Schedule

log = logging.getLogger(__name__)
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
LAST_GEN_KEY = "last_generation_date"
TG_OFFSET_KEY = "telegram_offset"


def _at(day: datetime, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return day.replace(hour=h, minute=m, second=0, microsecond=0)


def publish_time_for_approval(schedule: Schedule, now_local: datetime) -> datetime:
    """Approved before today's slot -> today's slot; after it -> publish now."""
    slot = _at(now_local, schedule.publish_at)
    return slot if now_local < slot else now_local


def generation_due(schedule: Schedule, now_local: datetime, last_date: str | None) -> bool:
    if DAYS[now_local.weekday()] not in schedule.days:
        return False
    if last_date == now_local.date().isoformat():
        return False
    return now_local >= _at(now_local, schedule.generate_at)


class Worker:
    def __init__(self, pipeline: Pipeline, db: DB, tz: str, poll_timeout: int = 30):
        self.p = pipeline
        self.db = db
        self.tz = ZoneInfo(tz)
        self.poll_timeout = poll_timeout
        self.tg = pipeline.notifier

    def now(self) -> datetime:
        return datetime.now(self.tz)

    # ---------------------------------------------------------------- generation
    def resume_interrupted(self) -> None:
        for job in self.db.jobs_with_status(JobStatus.PLANNED, JobStatus.GENERATING):
            log.info("resuming job %d", job["id"])
            self._safe_generate(job["id"])

    def _safe_generate(self, job_id: int) -> None:
        try:
            self.p.generate(job_id)
        except BudgetExceeded:
            log.warning("job %d hit the budget guard", job_id)
        except Exception:
            log.exception("job %d failed", job_id)

    def maybe_generate(self) -> int | None:
        now = self.now()
        if not generation_due(self.p.s.schedule, now, self.db.kv_get(LAST_GEN_KEY)):
            return None
        self.db.kv_set(LAST_GEN_KEY, now.date().isoformat())  # set first: never double-generate a day
        try:
            job_id = self.p.plan_next()
        except Exception as e:
            log.exception("planning failed")
            self.tg.send_message(f"⚠️ Planning failed: {e}"[:1000])
            return None
        self._safe_generate(job_id)
        return job_id

    # ---------------------------------------------------------------- telegram
    def handle_update(self, upd: dict[str, Any]) -> None:
        if cb := upd.get("callback_query"):
            self._handle_callback(cb)
        elif (msg := upd.get("message")) and msg.get("reply_to_message"):
            self._handle_reply(msg)

    def _authorized(self, chat: dict[str, Any] | None) -> bool:
        return chat is not None and str(chat.get("id")) == str(self.tg.chat_id)

    def _handle_callback(self, cb: dict[str, Any]) -> None:
        if not self._authorized((cb.get("message") or {}).get("chat")):
            log.warning("ignoring callback from unauthorized chat")
            return
        parts = (cb.get("data") or "").split(":")
        action, job_id = parts[0], int(parts[1])
        msg_id = cb["message"]["message_id"]
        try:
            if action == "a":
                when = publish_time_for_approval(self.p.s.schedule, self.now())
                self.p.approve(job_id, when)
                self.tg.answer_callback(cb["id"], "Approved")
                self.tg.clear_keyboard(msg_id)
                self.tg.send_message(f"✅ #{job_id} approved, posting at {when:%a %H:%M}", reply_to=msg_id)
            elif action == "x":
                self.p.reject(job_id)
                self.tg.answer_callback(cb["id"], "Rejected")
                self.tg.clear_keyboard(msg_id)
                self.tg.send_message(f"❌ #{job_id} rejected", reply_to=msg_id)
            elif action == "s":
                idx = int(parts[2])
                self.tg.answer_callback(cb["id"], f"Regenerating shot {idx + 1}…")
                self.tg.clear_keyboard(msg_id)
                self.tg.send_message(f"🔁 Re-rendering shot {idx + 1} of #{job_id}…", reply_to=msg_id)
                self.p.regenerate_shot(job_id, idx)
            elif action == "m":
                self.tg.answer_callback(cb["id"], "New music…")
                self.tg.clear_keyboard(msg_id)
                self.tg.send_message(f"🎵 New music for #{job_id}…", reply_to=msg_id)
                self.p.regenerate_music(job_id)
        except (ValueError, KeyError) as e:
            self.tg.answer_callback(cb["id"], str(e))
            self.tg.send_message(f"⚠️ {e}", reply_to=msg_id)
        except BudgetExceeded as e:
            self.tg.send_message(f"⛔ {e}")
        except Exception:
            log.exception("callback %s failed", cb.get("data"))

    def _handle_reply(self, msg: dict[str, Any]) -> None:
        if not self._authorized(msg.get("chat")) or not msg.get("text"):
            return
        replied = msg["reply_to_message"]["message_id"]
        for job in self.db.jobs_with_status(JobStatus.AWAITING_REVIEW, JobStatus.APPROVED):
            if job["tg_message_id"] == replied:
                caption = self.p.set_hook(job["id"], msg["text"])
                self.tg.send_message(f"✏️ Caption for #{job['id']} updated:\n\n{caption}", reply_to=replied)
                return

    def poll_telegram(self) -> None:
        offset = self.db.kv_get(TG_OFFSET_KEY)
        updates = self.tg.get_updates(int(offset) if offset else None, timeout=self.poll_timeout)
        for upd in updates:
            self.db.kv_set(TG_OFFSET_KEY, str(upd["update_id"] + 1))  # ack before handling: no replays
            self.handle_update(upd)

    # ---------------------------------------------------------------- loop
    def tick(self) -> None:
        self.maybe_generate()
        self.p.publish_due(datetime.now(UTC))
        self.poll_telegram()

    def run_once(self) -> None:
        """One pass for cron hosts (GitHub Actions): finish crashed renders, drain Telegram
        (approvals first, so a late approval can publish in the same run), generate, publish."""
        self.resume_interrupted()
        self.poll_telegram()
        self.maybe_generate()
        self.p.publish_due(datetime.now(UTC))

    def run_forever(self) -> None:
        log.info("worker started (tz=%s)", self.tz)
        self.resume_interrupted()
        backoff = 5
        while True:
            try:
                self.tick()
                backoff = 5
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("tick failed; retrying in %ss", backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)
            if not hasattr(self.tg, "base"):  # console notifier has no long poll; don't spin
                time.sleep(20)
