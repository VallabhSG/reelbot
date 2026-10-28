import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from reelbot.cli import main
from reelbot.factory import build
from reelbot.instagram import TOKEN_KEY, InsightsUnavailable, InstagramPublisher
from reelbot.learn import (
    INSIGHTS_OFF_KEY,
    LESSONS_KEY,
    Learner,
    parse_note,
    planner_guidance,
    posted,
    ranked,
)
from reelbot.models import JobStatus
from reelbot.worker import Worker

IST = ZoneInfo("Asia/Kolkata")
G = "https://graph.instagram.com"


@pytest.fixture
def app(dry_settings):
    return build(dry_settings)


def _job(app, status=JobStatus.AWAITING_REVIEW, **fields):
    plan = app.pipeline.planner.plan(app.db.next_episode_no(), [])
    jid = app.db.create_job(app.db.next_episode_no(), plan)
    app.db.update_job(jid, status=status, **fields)
    return jid


def _published(app, days_ago, media_id, watch_ms=None):
    when = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(timespec="seconds")
    jid = _job(app, JobStatus.PUBLISHED, ig_media_id=media_id, publish_after=when)
    if watch_ms is not None:
        app.db.save_metrics(jid, media_id, {"views": 1000.0, "ig_reels_avg_watch_time": float(watch_ms)})
    return jid


# ------------------------------------------------------------------ parsing + storage
@pytest.mark.parametrize(
    "text, expected",
    [
        ("#7 the slide made no sense", (7, "the slide made no sense")),
        ("7: opening too static", (7, "opening too static")),
        ("/note #12 more Mochi", (12, "more Mochi")),
        ("/note shorter hooks please", (None, "shorter hooks please")),
        ("make the cat talk less", (None, "make the cat talk less")),
    ],
)
def test_parse_note(text, expected):
    assert parse_note(text) == expected


def test_feedback_and_metrics_are_stored(db):
    assert db.last_evidence_at() == ""
    db.add_feedback(3, "note", "  too dark  ")
    db.save_metrics(3, "M3", {"views": 10.0})
    db.save_metrics(3, "M3", {"views": 25.0})  # refresh replaces
    assert db.feedback()[0]["text"] == "too dark"
    assert db.metrics()[3]["data"] == {"views": 25.0}
    assert db.last_evidence_at()


def test_verdicts_are_recorded_but_dead_renders_are_not_story_feedback(app):
    a, b, c = _job(app), _job(app), _job(app, JobStatus.FAILED)
    app.pipeline.approve(a, note="great payoff")
    app.pipeline.reject(b, note="the slide made no sense")
    app.pipeline.reject(c)  # out of credits: says nothing about the story
    kinds = [(f["job_id"], f["kind"], f["text"]) for f in app.db.feedback()]
    assert (a, "approved", "great payoff") in kinds and (b, "rejected", "the slide made no sense") in kinds
    assert all(j != c for j, _, _ in kinds)


# ------------------------------------------------------------------ Instagram insights
@respx.mock
def test_insights_parses_lifetime_values(db):
    respx.get(f"{G}/M1/insights").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"name": "views", "period": "lifetime", "values": [{"value": 2140}]},
                    {"name": "ig_reels_avg_watch_time", "values": [{"value": 9300}]},
                    {"name": "shares", "total_value": {"value": 12}},
                ]
            },
        )
    )
    out = InstagramPublisher("17841", "t", db).insights("M1")
    assert out == {"views": 2140.0, "ig_reels_avg_watch_time": 9300.0, "shares": 12.0}


@respx.mock
def test_one_unsupported_metric_does_not_lose_the_others(db):
    def answer(request):
        metric = request.url.params["metric"]
        if "," in metric or metric == "reels_skip_rate":
            return httpx.Response(
                400, json={"error": {"message": f"metric {metric} not supported", "code": 100}}
            )
        return httpx.Response(200, json={"data": [{"name": metric, "values": [{"value": 5}]}]})

    respx.get(f"{G}/M1/insights").mock(side_effect=answer)
    out = InstagramPublisher("17841", "t", db).insights("M1")
    assert out["views"] == 5.0 and "reels_skip_rate" not in out


@respx.mock
def test_missing_permission_is_reported_as_such(db):
    respx.get(f"{G}/M1/insights").mock(
        return_value=httpx.Response(
            403, json={"error": {"message": "(#10) Application does not have permission", "code": 10}}
        )
    )
    with pytest.raises(InsightsUnavailable):
        InstagramPublisher("17841", "t", db).insights("M1")


def test_a_new_env_token_replaces_the_refreshed_copy(db):
    db.kv_set(TOKEN_KEY, "refreshed-old")
    assert InstagramPublisher("1", "old-env", db).token == "refreshed-old"  # adopts the legacy copy
    assert InstagramPublisher("1", "old-env", db).token == "refreshed-old"
    assert InstagramPublisher("1", "new-env-with-insights", db).token == "new-env-with-insights"


# ------------------------------------------------------------------ metrics refresh
def test_metrics_refresh_daily_for_recent_reels_only(app):
    fresh = _published(app, 1, "M-fresh")
    _published(app, 30, "M-old")  # past the 14-day window
    learner = app.pipeline.learner
    assert learner.refresh_metrics() == 1
    assert set(app.db.metrics()) == {fresh}
    assert learner.refresh_metrics() == 0  # fetched less than a day ago


class NoInsights:
    def insights(self, media_id):
        raise InsightsUnavailable("(#10) no permission")


def test_missing_permission_nags_once_then_backs_off(app):
    _published(app, 1, "M1")
    learner = Learner(app.db, app.series, app.pipeline.planner.llm, NoInsights(), app.pipeline.notifier)
    before = len(app.pipeline.notifier.sent)
    assert learner.refresh_metrics() == 0
    assert "instagram_business_manage_insights" in app.pipeline.notifier.sent[-1]
    assert app.db.kv_get(INSIGHTS_OFF_KEY)
    assert learner.refresh_metrics() == 0  # backs off for a few days, no second message
    assert len(app.pipeline.notifier.sent) == before + 1


# ------------------------------------------------------------------ lessons
def test_notes_become_lessons_and_only_new_evidence_triggers_a_rewrite(app):
    learner = app.pipeline.learner
    assert learner.update_lessons() is None  # no evidence yet
    jid = _job(app)
    app.pipeline.add_note(jid, "the opening is too static")
    added, dropped = learner.update_lessons()
    assert added == ["Fix: the opening is too static"] and dropped == []
    assert learner.update_lessons() is None  # nothing new since
    assert "the opening is too static" in learner.lessons_text()


class BrokenLLM:
    def json_chat(self, *a, **k):
        raise RuntimeError("429")


def test_a_failed_rewrite_keeps_the_old_lessons(app):
    app.db.kv_set(LESSONS_KEY, json.dumps([{"text": "keep me", "evidence": "x"}]))
    app.pipeline.add_note(None, "new note")
    learner = Learner(app.db, app.series, BrokenLLM(), app.pipeline.publisher, app.pipeline.notifier)
    assert learner.update_lessons() is None
    assert "keep me" in learner.lessons_text()


def test_planner_gets_lessons_then_best_and_worst_reels(app):
    assert planner_guidance(app.db) == ""
    app.db.kv_set(LESSONS_KEY, json.dumps([{"text": "Open on motion", "evidence": "Val on #7"}]))
    g = planner_guidance(app.db)
    assert "LESSONS" in g and "- Open on motion" in g and "watched these the longest" not in g
    for days, ms in ((5, 9000), (4, 4000), (3, 12000)):
        _published(app, days, f"M{days}", ms)
    order = [r.data["ig_reels_avg_watch_time"] for r in ranked(posted(app.db))]
    assert order == [12000, 9000, 4000]
    g = planner_guidance(app.db)
    assert "watched these the longest" in g and "left this one early" in g


def test_guidance_reaches_the_storyboard_prompt(app):
    prompt = app.pipeline.planner.user_prompt(1, [], guidance="LESSONS FROM EARLIER EPISODES:\n- X")
    assert "- X" in prompt


# ------------------------------------------------------------------ Telegram + weekly report
def _msg(text, reply_to=None):
    m = {"message_id": 99, "chat": {"id": "console"}, "text": text}
    if reply_to:
        m["reply_to_message"] = reply_to
    return {"message": m}


def test_hash_message_in_telegram_is_a_note_with_the_new_lesson(app):
    jid = _job(app)
    w = Worker(app.pipeline, app.db, "Asia/Kolkata")
    w.handle_update(_msg(f"#{jid} the payoff came too late"))
    assert app.db.feedback()[0]["text"] == "the payoff came too late"
    assert f"Noted for #{jid}" in app.pipeline.notifier.sent[-1]
    assert "Fix: the payoff came too late" in app.pipeline.notifier.sent[-1]


def test_reply_to_a_live_message_is_a_note_but_reply_to_review_edits_the_caption(app):
    jid = _job(app, JobStatus.PUBLISHED)
    w = Worker(app.pipeline, app.db, "Asia/Kolkata")
    w.handle_update(_msg("loved the door slide", {"message_id": 5, "text": f"🚀 #{jid} is live: https://ig"}))
    assert (app.db.feedback()[0]["job_id"], app.db.feedback()[0]["text"]) == (jid, "loved the door slide")

    rid = _job(app)
    app.db.update_job(rid, tg_message_id=6)
    w.handle_update(_msg("new hook line", {"message_id": 6, "caption": f"#{rid} · Ep 2"}))
    assert app.db.feedback()[0]["job_id"] == jid  # no note: it edited the caption instead
    assert "new hook line" in app.db.get_job(rid)["caption"]


def test_weekly_report_once_on_sunday_evening(app):
    _published(app, 2, "M1", 9000)
    learner = app.pipeline.learner
    sat = datetime(2026, 10, 31, 19, 0, tzinfo=IST)
    sun_noon = datetime(2026, 11, 1, 12, 0, tzinfo=IST)
    sun_eve = datetime(2026, 11, 1, 18, 30, tzinfo=IST)
    assert not learner.weekly_report(sat) and not learner.weekly_report(sun_noon)
    assert learner.weekly_report(sun_eve)
    report = app.pipeline.notifier.sent[-1]
    assert report.startswith("📊 Week 44") and "Best: #" in report and "Lessons" in report
    assert not learner.weekly_report(sun_eve + timedelta(hours=1))


def test_tick_summary_includes_learning(app, monkeypatch):
    _published(app, 1, "M1")
    w = Worker(app.pipeline, app.db, "Asia/Kolkata", poll_timeout=0)
    monkeypatch.setattr(w, "now", lambda: datetime(2026, 10, 29, 11, 0, tzinfo=IST))  # Thursday
    assert "learn: 1 reel(s) measured" in w.run_once()


def test_note_lessons_and_stats_commands(dry_settings, monkeypatch, capsys):
    monkeypatch.setattr("reelbot.cli.get_settings", lambda: dry_settings)
    app = build(dry_settings)
    jid = _job(app)
    _published(app, 1, "M9")
    assert main(["note", str(jid), "the", "cat", "should", "react"]) == 0
    assert main(["lessons"]) == 0
    assert "Fix: the cat should react" in capsys.readouterr().out
    assert main(["reject", str(jid), "payoff", "unclear"]) == 0
    assert build(dry_settings).db.feedback()[0]["text"] == "payoff unclear"
    assert main(["stats"]) == 0
    assert "views" in capsys.readouterr().out
