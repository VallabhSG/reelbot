"""The learning loop: every reel teaches the next one.

Evidence:  your verdicts and notes (approve / reject / retake / "note 7 the slide made no sense")
           + Instagram metrics of every posted reel (views, % watched, skip rate, shares, saves)
Notebook:  an LLM keeps a short list of LESSONS distilled from that evidence. Your notes outrank
           metrics; metrics from fewer than 3 reels count as an early signal only.
Use:       the planner gets the lessons plus the best / weakest posted episodes with every storyboard
           it writes, so stories drift toward what you approve and what viewers finish watching.
Report:    a weekly summary in Telegram (views, % watched vs last week, best reel, the lessons).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any

from .db import DB
from .models import EpisodePlan, JobStatus
from .series import Series

log = logging.getLogger(__name__)

LESSONS_KEY = "lessons"  # JSON: [{"text": str, "evidence": str}]
LESSONS_AT_KEY = "lessons_at"  # when they were last rewritten (UTC iso)
INSIGHTS_OFF_KEY = "insights_off"  # the token can't read insights: when we last found out (UTC iso)
REPORT_WEEK_KEY = "report_week"  # ISO week of the last weekly report

MAX_LESSONS = 10
METRICS_EVERY = timedelta(hours=20)  # refresh a posted reel's numbers about once a day...
METRICS_FOR = timedelta(days=14)  # ...for its first two weeks (most reels settle by then)
METRICS_SETTLED = timedelta(hours=48)  # Instagram's numbers can lag up to 48h
MIN_REELS_FOR_RANKING = 3
INSIGHTS_RETRY = timedelta(days=3)
REPORT_DAY, REPORT_HOUR = 6, 18  # Sunday, 18:00 local

NOTEBOOK_SYSTEM = """You keep the writers' room NOTEBOOK for "{name}", an anime micro-film series
on Instagram Reels ({seconds}s per episode). Premise: {premise}

From the evidence below, write the LESSONS the writer/director must follow in the next episodes.

Rules:
- At most {max} lessons, most important first. Each is ONE concrete instruction about what goes on
  screen: the stake, the hook's first second, the winning move, the payoff, timing, framing, the
  spoken lines, sound. Never generic advice ("be engaging", "make it funny").
- Every lesson must come from the evidence; cite it briefly in "evidence" (e.g. "Val on #7",
  "#9 kept 81% watched vs 55% median").
- The owner's notes and rejections outrank metrics. A rejection without a note is weak evidence.
- Metrics from fewer than {min_reels} posted reels are an early signal: say so in the evidence.
- Keep earlier lessons that still hold WORD FOR WORD; rewrite or drop only what newer evidence
  contradicts. With no real evidence for something, leave it out: fewer, true lessons beat many.
- Never name real films, brands or people.

JSON only: {{"lessons": [{{"text": str, "evidence": str}}]}}"""


@dataclass
class Posted:
    job_id: int
    episode_no: int
    plan: EpisodePlan
    published: datetime
    data: dict[str, float]
    fetched_at: str

    @property
    def seconds(self) -> float:
        return float(sum(s.seconds for s in self.plan.shots)) or 15.0

    @property
    def watched(self) -> float | None:
        """Average share of the reel watched, 0-1 (can exceed 1 with rewatches)."""
        ms = self.data.get("ig_reels_avg_watch_time")
        return ms / 1000 / self.seconds if ms is not None else None

    @property
    def settled(self) -> bool:
        return datetime.now(UTC) - self.published >= METRICS_SETTLED

    def line(self) -> str:
        d = self.data
        bits = [f"{int(d['views']):,} views" if "views" in d else "views n/a"]
        if self.watched is not None:
            bits.append(f"{self.watched:.0%} watched on average")
        if "reels_skip_rate" in d:
            bits.append(f"{d['reels_skip_rate']:.0f}% skipped")
        for k, label in (("shares", "shares"), ("saved", "saves"), ("comments", "comments")):
            if k in d:
                bits.append(f"{int(d[k])} {label}")
        age = (datetime.now(UTC) - self.published).days
        return (
            f"#{self.job_id} Ep {self.episode_no} {self.plan.title}: {', '.join(bits)}"
            f" ({age}d old{'' if self.settled else ', numbers still coming in'})"
        )

    def story(self) -> str:
        return f"{self.plan.logline} Winning move: {self.plan.winning_move or '-'}. Hook: {self.plan.hook!r}"


def parse_note(text: str) -> tuple[int | None, str]:
    """'#7 the slide made no sense' / '7 the slide…' / '/note #7 …' -> (7, 'the slide made no sense');
    '/note shorter hooks please' -> (None, 'shorter hooks please')."""
    t = text.strip()
    if t.lower().startswith("/note"):
        t = t[5:].strip()
    head, _, rest = t.partition(" ")
    num = head.lstrip("#").rstrip(":,.-")
    if num.isdigit():
        return int(num), rest.strip(" :,-")
    return None, t


def _parse_time(iso: str | None) -> datetime | None:
    if not iso:
        return None
    t = datetime.fromisoformat(iso)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def load_lessons(db: DB) -> list[dict[str, str]]:
    try:
        raw = json.loads(db.kv_get(LESSONS_KEY) or "[]")
    except json.JSONDecodeError:
        return []
    return [x for x in raw if isinstance(x, dict) and x.get("text")]


def posted(db: DB) -> list[Posted]:
    """Published reels that have metrics, oldest first."""
    metrics = db.metrics()
    out = []
    for job in db.jobs_with_status(JobStatus.PUBLISHED):
        m = metrics.get(job["id"])
        if not m:
            continue
        try:
            plan = EpisodePlan.model_validate_json(job["plan_json"])
        except Exception:  # an old-format plan can't teach the current format anything
            continue
        published = _parse_time(job["publish_after"]) or _parse_time(job["updated_at"]) or datetime.now(UTC)
        out.append(Posted(job["id"], job["episode_no"], plan, published, m["data"], m["fetched_at"]))
    return out


def ranked(reels: list[Posted]) -> list[Posted]:
    """Best first, by how much of the reel people watched (independent of follower count), then by
    shares + saves per view. Only settled reels with a watch-time number count."""
    scored = [r for r in reels if r.settled and r.watched is not None]

    def key(r: Posted) -> tuple[float, float]:
        views = max(r.data.get("views", 0.0), 1.0)
        return (r.watched or 0.0, (r.data.get("shares", 0.0) + r.data.get("saved", 0.0)) / views)

    return sorted(scored, key=key, reverse=True)


def planner_guidance(db: DB) -> str:
    """What the planner must take from earlier episodes: the lessons, plus the best and weakest
    posted stories once there are enough to compare."""
    lines: list[str] = []
    lessons = load_lessons(db)
    if lessons:
        lines.append(
            "LESSONS FROM EARLIER EPISODES (from the owner's reviews and viewer numbers; follow them):"
        )
        lines += [f"- {x['text']}" for x in lessons]
    best = ranked(posted(db))
    if len(best) >= MIN_REELS_FOR_RANKING:
        lines.append("Viewers watched these the longest; learn from what they do (never copy the story):")
        lines += [f"- {r.plan.title}: {r.story()} ({r.watched:.0%} watched)" for r in best[:2]]
        worst = best[-1]
        lines.append(
            f"Viewers left this one early; avoid what it did: {worst.plan.title}: {worst.story()} "
            f"({worst.watched:.0%} watched)"
        )
    return "\n".join(lines)


PLATFORM_NAMES = {"instagram": "Instagram", "youtube": "YouTube", "facebook": "Facebook"}
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def released_at(db: DB, job: dict[str, Any], platform: str) -> datetime | None:
    """When a platform got the reel: its staggered slot, else the Instagram slot."""
    raw = db.kv_get(f"slots:{job['id']}")
    slots = json.loads(raw) if raw else {}
    return (
        _parse_time(slots.get(platform))
        or _parse_time(job["publish_after"])
        or _parse_time(job["updated_at"])
    )


def day_report(db: DB, tz: Any) -> str:
    """Average views per weekday per platform, from posts at least 48 h old. The point of
    schedule.rotate: after a few weeks this says which two days to keep."""
    now = datetime.now(UTC)
    by: dict[str, dict[int, list[float]]] = {}
    jobs = {j["id"]: j for j in db.jobs_with_status(JobStatus.PUBLISHED, JobStatus.APPROVED)}
    rows = [((jid, "instagram"), m) for jid, m in db.metrics().items()] + list(db.platform_metrics().items())
    for (jid, platform), m in rows:
        job, views = jobs.get(jid), m["data"].get("views")
        at = released_at(db, job, platform) if job else None
        if views is None or not at or now - at < METRICS_SETTLED:
            continue
        by.setdefault(platform, {}).setdefault(at.astimezone(tz).weekday(), []).append(views)
    if not by:
        return ""
    lines = ["📅 Views by posting day (avg, posts 48h+ old):"]
    thin = False
    for platform in ("instagram", "youtube", "facebook"):
        days = by.get(platform)
        if not days:
            continue
        avg = sorted(((sum(v) / len(v), d, len(v)) for d, v in days.items()), reverse=True)
        thin |= any(n < 3 for _, _, n in avg)
        cells = " · ".join(f"{WEEKDAYS[d]} {a:,.0f} ({n})" for a, d, n in avg)
        lines.append(f"{PLATFORM_NAMES[platform]}: {cells}")
    if thin:
        lines.append("(n) = posts; under 3 per day is still mostly noise.")
    return "\n".join(lines)


class Learner:
    def __init__(
        self,
        db: DB,
        series: Series,
        llm: Any,
        publisher: Any,
        notifier: Any,
        platforms: Any = None,
        tz: Any = UTC,
    ):
        self.db = db
        self.s = series
        self.llm = llm
        self.publisher = publisher
        self.notifier = notifier
        self.platforms = platforms or dict  # () -> {"youtube": client, "facebook": client}
        self.tz = tz

    # ---------------------------------------------------------------- evidence: metrics
    def refresh_metrics(self, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        return self._refresh_instagram(now) + self._refresh_crossposts(now)

    def _refresh_crossposts(self, now: datetime) -> int:
        """YouTube / Facebook numbers of recent cross-posts, about once a day each."""
        have, n = self.db.platform_metrics(), 0
        for platform, client in self.platforms().items():
            if not hasattr(client, "stats"):
                continue
            for job in self.db.jobs_with_status(JobStatus.PUBLISHED, JobStatus.APPROVED):
                post_id = self.db.kv_get(f"{platform}_id:{job['id']}")
                at = released_at(self.db, job, platform)
                if not post_id or not at or now - at > METRICS_FOR:
                    continue
                last = _parse_time((have.get((job["id"], platform)) or {}).get("fetched_at"))
                if last and now - last < METRICS_EVERY:
                    continue
                try:
                    data = client.stats(post_id)
                except Exception as e:  # numbers are nice to have; never break a run over them
                    log.warning("%s stats for #%s failed: %s", platform, job["id"], e)
                    continue
                if data:
                    self.db.save_platform_metrics(job["id"], platform, post_id, data)
                    n += 1
        return n

    def _refresh_instagram(self, now: datetime) -> int:
        """Fetch Instagram numbers for recently posted reels, about once a day each."""
        if not hasattr(self.publisher, "insights"):
            return 0
        off = _parse_time(self.db.kv_get(INSIGHTS_OFF_KEY))
        if off and now - off < INSIGHTS_RETRY:
            return 0
        from .instagram import InsightsUnavailable

        have = self.db.metrics()
        n = 0
        for job in self.db.jobs_with_status(JobStatus.PUBLISHED):
            media_id = job.get("ig_media_id")
            published = _parse_time(job["publish_after"]) or _parse_time(job["updated_at"])
            if not media_id or not published or now - published > METRICS_FOR:
                continue
            last = _parse_time((have.get(job["id"]) or {}).get("fetched_at"))
            if last and now - last < METRICS_EVERY:
                continue
            try:
                data = self.publisher.insights(media_id)
            except InsightsUnavailable as e:
                first_time = not self.db.kv_get(INSIGHTS_OFF_KEY)
                self.db.kv_set(INSIGHTS_OFF_KEY, now.isoformat(timespec="seconds"))
                log.warning("instagram insights unavailable: %s", e)
                if first_time:
                    self.notifier.send_message(
                        "📊 To learn from views and watch time, the Instagram token needs one more "
                        "permission: instagram_business_manage_insights. Meta app dashboard → Instagram → "
                        "API setup with Instagram login → add it, generate a new token, and put it in "
                        "ENV_FILE as IG_ACCESS_TOKEN. Until then the bot learns from your reviews only."
                    )
                return n
            except Exception as e:  # numbers are nice to have; never break a run over them
                log.warning("insights for #%s failed: %s", job["id"], e)
                continue
            if data:
                self.db.save_metrics(job["id"], media_id, data)
                n += 1
        if n and self.db.kv_get(INSIGHTS_OFF_KEY):
            self.db.kv_set(INSIGHTS_OFF_KEY, "")
        return n

    # ---------------------------------------------------------------- the notebook
    def evidence(self) -> str:
        parts = []
        lessons = load_lessons(self.db)
        if lessons:
            parts.append(
                "CURRENT LESSONS:\n" + "\n".join(f"- {x['text']} [{x.get('evidence', '')}]" for x in lessons)
            )
        fb = self.db.feedback(limit=40)
        if fb:
            lines = []
            for f in fb:
                job = self.db.get_job(f["job_id"]) if f["job_id"] else None
                head = f"#{f['job_id']}" if f["job_id"] else "general"
                if job:
                    try:
                        plan = EpisodePlan.model_validate_json(job["plan_json"])
                        head += f" Ep {job['episode_no']} {plan.title} ({plan.logline})"
                    except Exception:
                        pass
                verb = {
                    "note": "owner's note",
                    "approved": "approved",
                    "rejected": "REJECTED",
                    "retake": "asked for a new take",
                }.get(f["kind"], f["kind"])
                lines.append(
                    f"- {f['created_at'][:10]} {head}: {verb}" + (f': "{f["text"]}"' if f["text"] else "")
                )
            parts.append("OWNER FEEDBACK (newest first):\n" + "\n".join(lines))
        reels = posted(self.db)
        if reels:
            watched = [r.watched for r in reels if r.watched is not None and r.settled]
            mid = f" Median watched: {median(watched):.0%}." if watched else ""
            parts.append(
                f"POSTED REELS AND THEIR NUMBERS ({len(reels)} so far).{mid}\n"
                + "\n".join(f"- {r.line()}\n  story: {r.story()}" for r in reels[-12:])
            )
        return "\n\n".join(parts)

    def stale(self) -> bool:
        newest = self.db.last_evidence_at()
        return bool(newest) and newest > (self.db.kv_get(LESSONS_AT_KEY) or "")

    def update_lessons(self, force: bool = False) -> tuple[list[str], list[str]] | None:
        """Rewrite the lessons if there's new evidence. Returns (added, dropped), or None if nothing ran."""
        if not force and not self.stale():
            return None
        evidence = self.evidence()
        if not evidence:
            return None
        system = NOTEBOOK_SYSTEM.format(
            name=self.s.series.name,
            seconds=self.s.reel_seconds,
            premise=self.s.series.premise,
            max=MAX_LESSONS,
            min_reels=MIN_REELS_FOR_RANKING,
        )
        before = load_lessons(self.db)
        try:
            raw = self.llm.json_chat(system, evidence, temperature=0.3)
        except Exception as e:  # keep the old lessons; try again next run
            log.warning("lessons update failed: %s", e)
            return None
        lessons = [
            {"text": str(x.get("text", "")).strip(), "evidence": str(x.get("evidence", "")).strip()}
            for x in (raw.get("lessons") or [])
            if isinstance(x, dict) and str(x.get("text", "")).strip()
        ][:MAX_LESSONS]
        if not lessons and before:  # an empty answer is a glitch, not "forget everything"
            log.warning("lessons update returned nothing; keeping the old ones")
            return None
        self.db.kv_set(LESSONS_KEY, json.dumps(lessons))
        self.db.kv_set(LESSONS_AT_KEY, datetime.now(UTC).isoformat(timespec="seconds"))
        old, new = {x["text"] for x in before}, {x["text"] for x in lessons}
        added = [x["text"] for x in lessons if x["text"] not in old]
        dropped = [x["text"] for x in before if x["text"] not in new]
        log.info("lessons updated: %d (+%d -%d)", len(lessons), len(added), len(dropped))
        return added, dropped

    def lessons_text(self) -> str:
        lessons = load_lessons(self.db)
        if not lessons:
            return "No lessons yet: they come from your notes (note ID text) and the reels' numbers."
        return "\n".join(f"{i}. {x['text']}  ({x.get('evidence', '')})" for i, x in enumerate(lessons, 1))

    # ---------------------------------------------------------------- weekly report
    def weekly_report(self, now_local: datetime) -> bool:
        if now_local.weekday() != REPORT_DAY or now_local.hour < REPORT_HOUR:
            return False
        week = f"{now_local.isocalendar().year}-W{now_local.isocalendar().week:02d}"
        if self.db.kv_get(REPORT_WEEK_KEY) == week:
            return False
        reels = posted(self.db)
        published = self.db.jobs_with_status(JobStatus.PUBLISHED)
        if not published:
            return False
        self.db.kv_set(REPORT_WEEK_KEY, week)
        now = now_local.astimezone(UTC)

        def window(lo: int, hi: int) -> list[Posted]:
            return [r for r in reels if now - timedelta(days=hi) <= r.published < now - timedelta(days=lo)]

        def avg_watched(rs: list[Posted]) -> float | None:
            w = [r.watched for r in rs if r.watched is not None]
            return sum(w) / len(w) if w else None

        this, last = window(0, 7), window(7, 14)
        lines = [f"📊 Week {now_local.isocalendar().week} · {len(published)} reel(s) posted so far"]
        if this:
            views = sum(int(r.data.get("views", 0)) for r in this)
            w, wl = avg_watched(this), avg_watched(last)
            trend = f" (last week {wl:.0%})" if w is not None and wl is not None else ""
            lines.append(
                f"This week: {len(this)} reel(s) · {views:,} views"
                + (f" · {w:.0%} watched on average{trend}" if w is not None else "")
            )
            top = max(this, key=lambda r: (r.watched or 0, r.data.get("views", 0)))
            lines.append(f"Best: {top.line()}")
        elif not reels:
            lines.append("No viewer numbers yet (they need the insights permission, and up to 48h).")
        if days := day_report(self.db, self.tz):
            lines.append("\n" + days)
        self.update_lessons()
        lines.append("\n📒 Lessons the next episodes follow:\n" + self.lessons_text())
        self.notifier.send_message("\n".join(lines)[:3900])
        return True

    # ---------------------------------------------------------------- one pass
    def run(self, now_local: datetime) -> str:
        fetched = self.refresh_metrics(now_local.astimezone(UTC))
        reported = self.weekly_report(now_local)
        changed = None if reported else self.update_lessons()
        bits = []
        if fetched:
            bits.append(f"{fetched} reel(s) measured")
        if changed is not None:
            bits.append(f"lessons +{len(changed[0])} -{len(changed[1])}")
        if reported:
            bits.append("weekly report sent")
        return ", ".join(bits) or "nothing new"
