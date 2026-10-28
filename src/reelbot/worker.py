"""Long-running worker: daily generation on schedule, Telegram review loop, timed publishing.

Single-threaded on purpose: Telegram queues updates server-side, so long generations simply delay
button handling instead of racing it. State lives in SQLite, so restarts resume cleanly.
"""

from __future__ import annotations

import json
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
TG_SEEN_KEY = "telegram_seen"  # recent update ids: a crash between handling and confirming never replays
TG_SEEN_MAX = 300


def _describe(upd: dict[str, Any]) -> str:
    if cb := upd.get("callback_query"):
        chat = ((cb.get("message") or {}).get("chat") or {}).get("id")
        return f"button {cb.get('data')!r} from chat {chat}"
    msg = upd.get("message") or {}
    text = (msg.get("text") or "")[:40]
    kind = "reply" if msg.get("reply_to_message") else "message"
    return f"{kind} {text!r} from chat {(msg.get('chat') or {}).get('id')}"


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
        self.gen_note = ""  # why the last maybe_generate() did or didn't generate, for the tick summary

    def now(self) -> datetime:
        return datetime.now(self.tz)

    # ---------------------------------------------------------------- generation
    def resume_interrupted(self) -> list[int]:
        ids = []
        for job in self.db.jobs_with_status(JobStatus.PLANNED, JobStatus.GENERATING):
            log.info("resuming job %d", job["id"])
            self._safe_generate(job["id"])
            ids.append(job["id"])
        return ids

    def _safe_generate(self, job_id: int) -> None:
        try:
            self.p.generate(job_id)
        except BudgetExceeded:
            log.warning("job %d hit the budget guard", job_id)
        except Exception:
            log.exception("job %d failed", job_id)

    def maybe_generate(self) -> int | None:
        now = self.now()
        sched = self.p.s.schedule
        if not generation_due(sched, now, self.db.kv_get(LAST_GEN_KEY)):
            self.gen_note = f"not due (episodes {'/'.join(sched.days)} at {sched.generate_at})"
            return None
        if missing := self.p.cast_missing():
            self.gen_note = f"waiting for cast: {', '.join(missing)}"
            # don't burn the day: wait until the cast is approved, remind once per day
            nag_key = f"cast_nag:{now.date().isoformat()}"
            if not self.db.kv_get(nag_key):
                self.db.kv_set(nag_key, "1")
                self.tg.send_message(
                    f"🎭 Today's episode is waiting for the cast: no approved sheet for {', '.join(missing)}. "
                    "Send /cast here (or run the 'cast' workflow) and pick one per character."
                )
            return None
        self.db.kv_set(LAST_GEN_KEY, now.date().isoformat())  # set first: never double-generate a day
        try:
            job_id = self.p.plan_next()
        except Exception as e:
            log.exception("planning failed")
            self.tg.send_message(f"⚠️ Planning failed: {e}"[:1000])
            self.gen_note = "planning failed"
            return None
        self._safe_generate(job_id)
        self.gen_note = f"made job #{job_id}"
        return job_id

    # ---------------------------------------------------------------- telegram
    def handle_update(self, upd: dict[str, Any]) -> None:
        if cb := upd.get("callback_query"):
            self._handle_callback(cb)
        elif (msg := upd.get("message")) and msg.get("reply_to_message"):
            self._handle_reply(msg)
        elif (msg := upd.get("message")) and (msg.get("text") or "").strip().lower().startswith("/cast"):
            self._handle_cast_command(msg)

    def _handle_cast_command(self, msg: dict[str, Any]) -> None:
        if not self._authorized(msg.get("chat")):
            return
        args = msg["text"].split()[1:]
        names = [c.name for c in self.p.s.characters if c.name.lower() in {a.lower() for a in args}] or None
        self.tg.send_message("🎭 Drawing character sheets, they'll arrive here in a minute…")
        try:
            self.p.propose_cast(names)
        except BudgetExceeded as e:
            self.tg.send_message(f"⛔ {e}")
        except Exception as e:
            log.exception("cast failed")
            self.tg.send_message(f"⚠️ Cast failed: {e}"[:1000])

    def _authorized(self, chat: dict[str, Any] | None) -> bool:
        return chat is not None and str(chat.get("id")).strip() == str(self.tg.chat_id).strip()

    def _handle_callback(self, cb: dict[str, Any]) -> None:
        chat = (cb.get("message") or {}).get("chat")
        if not self._authorized(chat):
            got = (chat or {}).get("id")
            log.warning("ignoring button from chat %s (TELEGRAM_CHAT_ID is %s)", got, self.tg.chat_id)
            # tell the owner, so a mistyped TELEGRAM_CHAT_ID is never a silent dead end
            self.tg.send_message(
                f"⚠️ Ignored a button tap from chat {got}: TELEGRAM_CHAT_ID is set to {self.tg.chat_id}."
            )
            return
        parts = (cb.get("data") or "").split(":")
        action = parts[0]
        msg_id = cb["message"]["message_id"]
        if action in {"c", "cm"}:
            self._handle_cast_callback(cb, parts, msg_id)
            return
        job_id = int(parts[1])
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
        except Exception as e:
            log.exception("callback %s failed", cb.get("data"))
            self._report_failure(cb.get("data"), e, msg_id)

    def _handle_cast_callback(self, cb: dict[str, Any], parts: list[str], msg_id: int) -> None:
        name = parts[1]
        try:
            if parts[0] == "c":
                self.tg.answer_callback(cb["id"], f"{name} locked in")
                self.p.lock_cast(name, int(parts[2]), reply_to=msg_id)
            else:
                self.tg.answer_callback(cb["id"], f"More {name} options…")
                self.tg.clear_keyboard(msg_id)
                self.p.propose_cast([name])
        except (ValueError, KeyError) as e:
            self.tg.send_message(f"⚠️ {e}", reply_to=msg_id)
        except BudgetExceeded as e:
            self.tg.send_message(f"⛔ {e}")
        except Exception as e:
            log.exception("cast callback %s failed", cb.get("data"))
            self._report_failure(cb.get("data"), e, msg_id)

    def _report_failure(self, data: Any, e: Exception, msg_id: int) -> None:
        try:  # every tap gets a visible answer, even when the action itself blew up
            self.tg.send_message(f"⚠️ Button {data} failed: {type(e).__name__}: {e}"[:1000], reply_to=msg_id)
        except Exception:
            log.exception("could not report the failure to Telegram either")

    def _handle_reply(self, msg: dict[str, Any]) -> None:
        if not self._authorized(msg.get("chat")) or not msg.get("text"):
            return
        replied = msg["reply_to_message"]["message_id"]
        for job in self.db.jobs_with_status(JobStatus.AWAITING_REVIEW, JobStatus.APPROVED):
            if job["tg_message_id"] == replied:
                caption = self.p.set_hook(job["id"], msg["text"])
                self.tg.send_message(f"✏️ Caption for #{job['id']} updated:\n\n{caption}", reply_to=replied)
                return

    def poll_telegram(self) -> int:
        """Handle every tap/message Telegram still holds, then confirm them.

        No stored offset: we ask for everything unconfirmed, skip ids we've already handled, and only
        confirm after handling. A stale or wrong offset can't silently swallow taps this way, and a
        crash mid-batch replays nothing (handled ids are remembered first)."""
        updates = self.tg.get_updates(None, timeout=self.poll_timeout)
        log.info("telegram: %d pending update(s)", len(updates))
        seen: list[int] = json.loads(self.db.kv_get(TG_SEEN_KEY) or "[]")
        handled = 0
        for upd in updates:
            uid = upd["update_id"]
            if uid in seen:
                log.info("telegram update %s already handled; confirming only", uid)
                continue
            seen = [*seen, uid][-TG_SEEN_MAX:]
            self.db.kv_set(TG_SEEN_KEY, json.dumps(seen))
            log.info("telegram update %s: %s", uid, _describe(upd))
            try:
                self.handle_update(upd)
                handled += 1
            except Exception:  # one bad update must never stall the run (or the ones after it)
                log.exception("telegram update %s failed", uid)
        if updates:  # confirm: Telegram drops everything below this offset
            self.tg.get_updates(max(u["update_id"] for u in updates) + 1, timeout=0, limit=1)
        return handled

    # ---------------------------------------------------------------- loop
    def tick(self) -> None:
        self.maybe_generate()
        self.p.publish_due(datetime.now(UTC))
        self.poll_telegram()

    def run_once(self) -> str:
        """One pass for cron hosts (GitHub Actions): finish crashed renders, drain Telegram
        (approvals first, so a late approval can publish in the same run), generate, publish.
        Returns a one-line summary so every run's log says what it did."""
        resumed = self.resume_interrupted()
        try:
            taps: int | str = self.poll_telegram()
        except Exception as e:  # Telegram down must not block a due generation or publish
            log.exception("telegram poll failed")
            taps = f"poll failed ({type(e).__name__})"
        self.gen_note = ""
        self.maybe_generate()
        published = self.p.publish_due(datetime.now(UTC)) or []
        parts = [f"Telegram: {taps} handled" if isinstance(taps, int) else f"Telegram: {taps}"]
        if resumed:
            parts.append(f"resumed #{', #'.join(map(str, resumed))}")
        parts.append(f"generate: {self.gen_note or 'nothing'}")
        parts.append(f"published: {len(published)} reel(s)" if published else "publish: nothing due")
        return " · ".join(parts)

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
