"""reelbot CLI.

reelbot check                 validate config, show cost estimates, check ffmpeg + credentials
reelbot make [--job ID]       plan + generate one episode now (or resume a failed job) -> review
reelbot status                recent jobs + month spend
reelbot approve ID [--now]    approve (posts at schedule.publish_at, or immediately with --now)
reelbot reject ID
reelbot regen ID SHOT         regenerate shot number SHOT (1-based)
reelbot publish-due           publish approved jobs whose slot has passed
reelbot run                   long-running worker (schedule + Telegram review + publishing)
reelbot tick                  one worker pass, for cron hosts (GitHub Actions)
reelbot state pull|push       sync DB + in-flight renders with the private state bucket
reelbot smoke                 verify every live service (1 test image, no posting)
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .config import get_settings
from .factory import build
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
    print(f"✓ series '{series.series.name}' ({series.shots.count} shots × {series.shots.duration_s}s)")
    print(f"  image: {series.models.image.endpoint}")
    print(f"  video: {series.models.video.endpoint}")
    print(
        f"  est. ${per_reel:.2f}/reel (first take) · ~${per_month:.2f}/month at {len(series.schedule.days)}/week"
        f" · cap ${series.budget.monthly_usd:.2f}/month, ${series.budget.per_reel_usd:.2f}/reel"
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
        if series.music.provider == "elevenlabs" and not s.elevenlabs_api_key:
            print("✗ Music: missing ELEVENLABS_API_KEY")
            ok = False
        for kind, spec in (("image", series.models.image), ("video", series.models.video)):
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
    for j in jobs:
        plan = app.db.plan(j["id"])
        cost = app.db.spend_for_job(j["id"])
        print(
            f"#{j['id']:<4} ep{j['episode_no']:<4} {j['status']:<16} ${cost:5.2f}  {plan.title}"
            + (f"  ! {j['error'][:80]}" if j["error"] else "")
        )
    print(f"month spend: ${app.guard.month_spend():.2f} / ${app.series.budget.monthly_usd:.2f}")
    return 0


def cmd_approve(a: argparse.Namespace) -> int:
    app = build(get_settings())
    tz = ZoneInfo(app.settings.timezone)
    when = datetime.now(UTC) if a.now else publish_time_for_approval(app.series.schedule, datetime.now(tz))
    app.pipeline.approve(a.job, when)
    print(f"job {a.job} approved, publishes at {when.astimezone(tz):%Y-%m-%d %H:%M %Z}")
    if a.now:
        app.pipeline.publish_due()
    return 0


def cmd_reject(a: argparse.Namespace) -> int:
    build(get_settings()).pipeline.reject(a.job)
    print(f"job {a.job} rejected")
    return 0


def cmd_regen(a: argparse.Namespace) -> int:
    out = build(get_settings()).pipeline.regenerate_shot(a.job, a.shot - 1)
    print(f"job {a.job} re-cut: {out}")
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
    Worker(app.pipeline, app.db, app.settings.timezone, poll_timeout=0).run_once()
    return 0


def cmd_state(a: argparse.Namespace) -> int:
    from .state import StateSync

    sync = StateSync(get_settings())
    sync.pull() if a.action == "pull" else sync.push()
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
    p.add_argument("--now", action="store_true")
    p.set_defaults(fn=cmd_approve)
    p = sub.add_parser("reject")
    p.add_argument("job", type=int)
    p.set_defaults(fn=cmd_reject)
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
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
