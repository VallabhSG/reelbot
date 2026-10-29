"""reelbot CLI.

reelbot check                 validate config, show cost estimates, check ffmpeg + credentials
reelbot make [--job ID]       plan + generate one episode now (or resume a failed job) -> review
reelbot status                recent jobs + month spend
reelbot approve ID [NOTE…] [--now]  approve (posts at schedule.publish_at, or now with --now)
reelbot reject ID [NOTE…]     reject; the note (why) teaches the next episodes
reelbot note ID NOTE…         tell the bot what worked or didn't in an episode (no ID: the series)
reelbot lessons [--update]    what the bot has learned from your notes + the reels' numbers
reelbot stats                 fetch Instagram numbers for posted reels now and list them
reelbot youtube ID            post a reel to YouTube Shorts now (e.g. one posted before YouTube was set up)
reelbot regen ID SHOT         regenerate shot number SHOT (1-based)  [v2 shot reels]
reelbot retake ID [NOTE…]     render the whole take again             [v3 one-take reels]
reelbot bakeoff [MODEL ...]   one episode rendered on several video models, to pick a winner
reelbot publish-due           publish approved jobs whose slot has passed
reelbot run                   long-running worker (schedule + Telegram review + publishing)
reelbot tick                  one worker pass, for cron hosts (GitHub Actions)
reelbot state pull|push       sync DB + in-flight renders with the private state bucket
reelbot smoke                 verify every live service (1 test image, no posting)
reelbot cast [NAME ...]       draw character-sheet options and send them to Telegram to pick
reelbot pick NAME N [...]     lock in option N for a character without Telegram (e.g. pick Juno 3 Mochi 1)
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .config import get_settings
from .factory import build
from .learn import parse_note, posted
from .models import JobStatus
from .worker import Worker, publish_time_for_approval

REQUIRED_LIVE = {
    "LLM planner": ["llm_api_key"],
    "Higgsfield": ["hf_api_key_id", "hf_api_key_secret"],
    "Storage": [
        "s3_endpoint_url",
        "s3_access_key_id",
        "s3_secret_access_key",
        "s3_bucket",
        "s3_public_base_url",
    ],
    "Telegram": ["telegram_bot_token", "telegram_chat_id"],
    "Instagram": ["ig_user_id", "ig_access_token"],
}


def cmd_check(_: argparse.Namespace) -> int:
    from .series import load_series

    s = get_settings()
    ok = True
    try:
        series = load_series(s.series_file)
    except Exception as e:
        print(f"✗ series file {s.series_file}: {e}")
        return 1
    per_reel = series.estimate_reel_cost()
    per_month = per_reel * len(series.schedule.days) * 52 / 12
    if series.take.enabled:
        t = series.take
        print(f"✓ series '{series.series.name}' (one take: {t.seconds}s, {t.shots} shots, cuts inside)")
        print(f"  image: {series.models.image.endpoint}")
        for name in series.take_model_names():
            spec = series.models.takes[name]
            role = "default" if name == t.model else "bake-off"
            print(
                f"  take [{role}] {spec.label or name}: {spec.endpoint} "
                f"~${series.estimate_reel_cost(name):.2f}/reel"
            )
    else:
        print(f"✓ series '{series.series.name}' ({series.shots.count} shots × {series.shots.duration_s}s)")
        print(f"  image: {series.models.image.endpoint}")
        print(f"  video: {series.models.video.endpoint}")
    print(
        f"  est. ${per_reel:.2f}/reel (first take) · ~${per_month:.2f}/month at {len(series.schedule.days)}/week"
        f" · cap ${series.budget.monthly_usd:.2f}/month, ${series.budget.per_reel_usd:.2f}/reel"
    )
    if series.characters:
        print(
            f"  cast sheets (one-time): ~${series.estimate_cast_cost():.2f} for "
            f"{len(series.characters)} characters × {series.cast.candidates} options"
        )
    if per_month > series.budget.monthly_usd:
        print("  ! schedule would exceed the monthly cap before month end; the guard will stop generation")
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            print(f"✗ {tool} not found on PATH")
            ok = False
    if s.dry_run:
        print("✓ DRY_RUN=true: stub providers, no credentials needed")
    else:
        for group, fields in REQUIRED_LIVE.items():
            missing = [f.upper() for f in fields if not getattr(s, f)]
            print(
                f"{'✗' if missing else '✓'} {group}" + (f": missing {', '.join(missing)}" if missing else "")
            )
            ok &= not missing
        if not series.youtube.enabled:
            print("– YouTube Shorts: off in the series file (youtube.enabled)")
        elif s.youtube_enabled:
            print("✓ YouTube Shorts: every reel is also posted as a Short")
        else:
            print(
                "– YouTube Shorts: off (set YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN)"
            )
        if series.music.provider == "elevenlabs" and not s.elevenlabs_api_key:
            print("✗ Music: missing ELEVENLABS_API_KEY")
            ok = False
        specs = [("image", series.models.image), ("video", series.models.video)]
        specs += [(f"takes.{n}", series.models.takes[n]) for n in series.take_model_names()]
        for kind, spec in specs:
            if "<" in spec.endpoint:
                print(f"✗ models.{kind}.endpoint is still a placeholder: {spec.endpoint}")
                ok = False
    for ch in series.characters:
        for p in ch.reference_images:
            if not series.resolve(p).exists():
                print(f"✗ character '{ch.name}' reference image not found: {p}")
                ok = False
    return 0 if ok else 1


def cmd_make(a: argparse.Namespace) -> int:
    app = build(get_settings())
    job_id = a.job or app.pipeline.plan_next()
    out = app.pipeline.generate(job_id)
    print(f"job {job_id} ready for review: {out}")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    app = build(get_settings())
    statuses = list(JobStatus)
    jobs = app.db.jobs_with_status(*statuses)[-15:]
    views = {
        r.job_id: f"{int(r.data.get('views', 0)):,} views"
        + (f", {r.watched:.0%} watched" if r.watched is not None else "")
        for r in posted(app.db)
    }
    for j in jobs:
        title = json.loads(j["plan_json"]).get("title", "?")  # raw: old-format plans still list
        cost = app.db.spend_for_job(j["id"])
        print(
            f"#{j['id']:<4} ep{j['episode_no']:<4} {j['status']:<16} ${cost:5.2f}  {title}"
            + (f"  ! {j['error'][:80]}" if j["error"] else "")
            + (f"  · {views[j['id']]}" if j["id"] in views else "")
        )
    print(f"month spend: ${app.guard.month_spend():.2f} / ${app.series.budget.monthly_usd:.2f}")
    cast = app.pipeline.cast_urls()
    print(
        "cast: "
        + ", ".join(f"{c.name} {'✓' if c.name in cast else '✗ (run cast)'}" for c in app.series.characters)
    )
    return 0


def _tell(app, job_id: int, text: str) -> None:
    """Mirror a command-line action in Telegram: drop the job's review buttons, say what happened."""
    job = app.db.get_job(job_id) or {}
    try:
        if job.get("tg_message_id"):
            app.pipeline.notifier.clear_keyboard(job["tg_message_id"])
        app.pipeline.notifier.send_message(text, reply_to=job.get("tg_message_id"))
    except Exception as e:  # the action itself already happened; Telegram is just the receipt
        print(f"(couldn't confirm in Telegram: {e})")


def cmd_approve(a: argparse.Namespace) -> int:
    app = build(get_settings())
    tz = ZoneInfo(app.settings.timezone)
    when = datetime.now(UTC) if a.now else publish_time_for_approval(app.series.schedule, datetime.now(tz))
    app.pipeline.approve(a.job, when, note=" ".join(a.note))
    print(f"job {a.job} approved, publishes at {when.astimezone(tz):%Y-%m-%d %H:%M %Z}")
    _tell(app, a.job, f"✅ #{a.job} approved, posting at {when.astimezone(tz):%a %H:%M}")
    if a.now:
        app.pipeline.publish_due()
    return 0


def cmd_reject(a: argparse.Namespace) -> int:
    app = build(get_settings())
    note = " ".join(a.note)
    app.pipeline.reject(a.job, note=note)
    print(f"job {a.job} rejected")
    _tell(app, a.job, f"❌ #{a.job} rejected" + (f": {note}" if note else ""))
    return 0


def cmd_note(a: argparse.Namespace) -> int:
    app = build(get_settings())
    job_id, text = parse_note(" ".join(a.words))
    app.pipeline.add_note(job_id, text)
    changed = app.pipeline.learner.update_lessons()
    msg = f"📝 Noted{f' for #{job_id}' if job_id else ''}: {text}"
    if changed and changed[0]:
        msg += "\nNew lesson(s) for the next episodes:\n" + "\n".join(f"• {t}" for t in changed[0])
    print(msg)
    try:
        app.pipeline.notifier.send_message(msg)
    except Exception as e:
        print(f"(couldn't confirm in Telegram: {e})")
    return 0


def cmd_lessons(a: argparse.Namespace) -> int:
    app = build(get_settings())
    if a.update:
        app.pipeline.learner.update_lessons(force=True)
    print(app.pipeline.learner.lessons_text())
    return 0


def cmd_youtube(a: argparse.Namespace) -> int:
    app = build(get_settings())
    app.db.kv_set(f"youtube_tries:{a.job}", "0")
    video_id = app.pipeline.post_youtube(a.job)
    print(f"job {a.job} on YouTube Shorts: https://youtube.com/shorts/{video_id}")
    return 0


def cmd_stats(_: argparse.Namespace) -> int:
    app = build(get_settings())
    n = app.pipeline.learner.refresh_metrics()
    reels = posted(app.db)
    print(f"fetched {n} reel(s) now; {len(reels)} with numbers:")
    for r in reels:
        print("  " + r.line())
    if not reels:
        print(
            "  none yet (posted reels need the instagram_business_manage_insights permission, and up to 48h)"
        )
    return 0


def parse_picks(tokens: list[str], names: list[str]) -> list[tuple[str, int]]:
    """'Juno 3 Mochi 1' or 'juno=3 mochi=1' -> [('Juno', 2), ('Mochi', 0)] (0-based)."""
    flat = [t for tok in tokens for t in tok.replace("=", " ").replace(",", " ").split()]
    if not flat or len(flat) % 2:
        raise SystemExit("usage: reelbot pick NAME OPTION [NAME OPTION ...]   e.g. pick Juno 3 Mochi 1")
    by_lower = {n.lower(): n for n in names}
    out = []
    for raw_name, raw_opt in zip(flat[::2], flat[1::2], strict=True):
        name = by_lower.get(raw_name.lower())
        if not name:
            raise SystemExit(f"unknown character {raw_name!r}; this series has: {', '.join(names)}")
        if not raw_opt.isdigit() or int(raw_opt) < 1:
            raise SystemExit(f"option for {name} must be a number like 1, 2, 3 (got {raw_opt!r})")
        out.append((name, int(raw_opt) - 1))
    return out


def cmd_pick(a: argparse.Namespace) -> int:
    app = build(get_settings())
    for name, idx in parse_picks(a.picks, [c.name for c in app.series.characters]):
        print(app.pipeline.lock_cast(name, idx))
    return 0


def cmd_regen(a: argparse.Namespace) -> int:
    out = build(get_settings()).pipeline.regenerate_shot(a.job, a.shot - 1)
    print(f"job {a.job} re-cut: {out}")
    return 0


def cmd_retake(a: argparse.Namespace) -> int:
    out = build(get_settings()).pipeline.retake(a.job, note=" ".join(a.note))
    print(f"job {a.job} new take: {out}")
    return 0


def cmd_bakeoff(a: argparse.Namespace) -> int:
    app = build(get_settings())
    ids = app.pipeline.bakeoff(a.models or None)
    for jid in ids:
        job = app.db.get_job(jid)
        print(
            f"#{jid} {app.pipeline.model_label(app.pipeline.take_model_for(jid)):<14} {job['status']:<16} "
            f"${app.db.spend_for_job(jid):.2f}" + (f"  ! {job['error'][:100]}" if job["error"] else "")
        )
    return 0


def cmd_publish_due(_: argparse.Namespace) -> int:
    ids = build(get_settings()).pipeline.publish_due()
    print(f"published: {ids or 'nothing due'}")
    return 0


def cmd_run(_: argparse.Namespace) -> int:
    app = build(get_settings())
    Worker(app.pipeline, app.db, app.settings.timezone).run_forever()
    return 0


def cmd_tick(_: argparse.Namespace) -> int:
    app = build(get_settings())
    print("tick: " + Worker(app.pipeline, app.db, app.settings.timezone, poll_timeout=0).run_once())
    return 0


def cmd_state(a: argparse.Namespace) -> int:
    from .state import StateSync

    sync = StateSync(get_settings())
    sync.pull() if a.action == "pull" else sync.push()
    return 0


def cmd_cast(a: argparse.Namespace) -> int:
    app = build(get_settings())
    names = [c.name for c in app.series.characters if c.name.lower() in {n.lower() for n in a.names}] or None
    out = app.pipeline.propose_cast(names)
    print("sent options to Telegram: " + ", ".join(f"{k} ×{len(v)}" for k, v in out.items()))
    return 0


def cmd_smoke(_: argparse.Namespace) -> int:
    from .smoke import run_smoke

    return run_smoke(get_settings())


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(
        prog="reelbot", description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    p = sub.add_parser("make")
    p.add_argument("--job", type=int)
    p.set_defaults(fn=cmd_make)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    p = sub.add_parser("approve")
    p.add_argument("job", type=int)
    p.add_argument("note", nargs="*", help="optional: what worked")
    p.add_argument("--now", action="store_true")
    p.set_defaults(fn=cmd_approve)
    p = sub.add_parser("reject")
    p.add_argument("job", type=int)
    p.add_argument("note", nargs="*", help="optional: why (it teaches the next episodes)")
    p.set_defaults(fn=cmd_reject)
    p = sub.add_parser("note")
    p.add_argument("words", nargs="+", help="[ID] what worked or didn't, e.g. 7 the slide made no sense")
    p.set_defaults(fn=cmd_note)
    p = sub.add_parser("lessons")
    p.add_argument("--update", action="store_true", help="rewrite them now from all the evidence")
    p.set_defaults(fn=cmd_lessons)
    sub.add_parser("stats").set_defaults(fn=cmd_stats)
    p = sub.add_parser("youtube")
    p.add_argument("job", type=int)
    p.set_defaults(fn=cmd_youtube)
    p = sub.add_parser("regen")
    p.add_argument("job", type=int)
    p.add_argument("shot", type=int)
    p.set_defaults(fn=cmd_regen)
    sub.add_parser("publish-due").set_defaults(fn=cmd_publish_due)
    sub.add_parser("run").set_defaults(fn=cmd_run)
    sub.add_parser("tick").set_defaults(fn=cmd_tick)
    p = sub.add_parser("state")
    p.add_argument("action", choices=["pull", "push"])
    p.set_defaults(fn=cmd_state)
    sub.add_parser("smoke").set_defaults(fn=cmd_smoke)
    p = sub.add_parser("cast")
    p.add_argument("names", nargs="*")
    p.set_defaults(fn=cmd_cast)
    p = sub.add_parser("retake")
    p.add_argument("job", type=int)
    p.add_argument("note", nargs="*", help="optional: what was wrong with the take")
    p.set_defaults(fn=cmd_retake)
    p = sub.add_parser("bakeoff")
    p.add_argument("models", nargs="*", help="keys from models.takes (default: take.bakeoff)")
    p.set_defaults(fn=cmd_bakeoff)
    p = sub.add_parser("pick")
    p.add_argument("picks", nargs="+", help="NAME OPTION pairs, e.g. Juno 3 Mochi 1")
    p.set_defaults(fn=cmd_pick)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
