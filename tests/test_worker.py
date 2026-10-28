from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from reelbot.series import Schedule
from reelbot.worker import Worker, generation_due, publish_time_for_approval

IST = ZoneInfo("Asia/Kolkata")


def test_generation_due_respects_time_day_and_once_per_day():
    s = Schedule(generate_at="10:00", days=["wed"])
    wed_0959 = datetime(2026, 10, 28, 9, 59, tzinfo=IST)
    wed_1001 = datetime(2026, 10, 28, 10, 1, tzinfo=IST)
    thu_1100 = datetime(2026, 10, 29, 11, 0, tzinfo=IST)
    assert not generation_due(s, wed_0959, None)
    assert generation_due(s, wed_1001, None)
    assert not generation_due(s, wed_1001, "2026-10-28")
    assert not generation_due(s, thu_1100, None)


def test_publish_time_for_approval():
    s = Schedule(publish_at="19:30")
    morning = datetime(2026, 10, 28, 11, 0, tzinfo=IST)
    night = datetime(2026, 10, 28, 21, 0, tzinfo=IST)
    assert publish_time_for_approval(s, morning) == datetime(2026, 10, 28, 19, 30, tzinfo=IST)
    assert publish_time_for_approval(s, night) == night


class FakeTG:
    chat_id = "42"

    def __init__(self):
        self.answers, self.messages, self.cleared = [], [], []

    def answer_callback(self, cid, text=""):
        self.answers.append(text)

    def send_message(self, text, reply_markup=None, reply_to=None):
        self.messages.append(text)
        return 1

    def clear_keyboard(self, mid):
        self.cleared.append(mid)

    def get_updates(self, offset, timeout=50, limit=100):
        return []


class QueueTG(FakeTG):
    """Telegram's real queue semantics: getUpdates(offset=k) confirms (drops) every update below k."""

    def __init__(self, updates):
        super().__init__()
        self.queue = list(updates)
        self.calls = []

    def get_updates(self, offset, timeout=50, limit=100):
        self.calls.append(offset)
        if offset is not None:
            self.queue = [u for u in self.queue if u["update_id"] >= offset]
        return self.queue[:limit]


class FakePipeline:
    def __init__(self, series):
        self.s = series
        self.notifier = FakeTG()
        self.calls = []

    def approve(self, job_id, when):
        self.calls.append(("approve", job_id))

    def reject(self, job_id):
        self.calls.append(("reject", job_id))

    def regenerate_shot(self, job_id, idx):
        self.calls.append(("regen", job_id, idx))

    def regenerate_music(self, job_id):
        self.calls.append(("music", job_id))

    def retake(self, job_id):
        self.calls.append(("retake", job_id))

    def pick_winner(self, job_id):
        self.calls.append(("winner", job_id))
        return "seedance2", [job_id - 1]

    def model_label(self, name):
        return {"seedance2": "Seedance 2.0"}.get(name, name)


def _cb(data, chat_id=42):
    return {
        "callback_query": {"id": "c", "data": data, "message": {"message_id": 9, "chat": {"id": chat_id}}}
    }


def test_callbacks_dispatch_and_unauthorized_chat_is_ignored(series, db):
    p = FakePipeline(series)
    w = Worker(p, db, "Asia/Kolkata")
    w.handle_update(_cb("a:3"))
    w.handle_update(_cb("s:3:1"))
    w.handle_update(_cb("m:3"))
    w.handle_update(_cb("x:3"))
    w.handle_update(_cb("a:3", chat_id=666))
    assert p.calls == [("approve", 3), ("regen", 3, 1), ("music", 3), ("reject", 3)]
    assert p.notifier.cleared == [9, 9, 9, 9]
    # the stranger's tap did nothing, but the owner is told (a mistyped chat id is never silent)
    assert "Ignored a button tap from chat 666" in p.notifier.messages[-1]


def test_retake_and_winner_buttons(series, db):
    p = FakePipeline(series)
    w = Worker(p, db, "Asia/Kolkata")
    w.handle_update(_cb("t:5"))
    w.handle_update(_cb("w:5"))
    assert p.calls == [("retake", 5), ("winner", 5)]
    assert p.notifier.messages[-1] == (
        "🏆 Seedance 2.0 is now the model for every reel. Rejected #4. Tap ✅ Approve on #5 to post it."
    )


def test_chat_id_compare_tolerates_whitespace(series, db):
    p = FakePipeline(series)
    p.notifier.chat_id = " 42 "
    Worker(p, db, "Asia/Kolkata").handle_update(_cb("x:3"))
    assert p.calls == [("reject", 3)]


def test_unexpected_failure_still_answers_in_telegram(series, db):
    p = FakePipeline(series)

    def boom(job_id):
        raise RuntimeError("disk full")

    p.reject = boom
    Worker(p, db, "Asia/Kolkata").handle_update(_cb("x:3"))
    assert p.notifier.messages[-1] == "⚠️ Button x:3 failed: RuntimeError: disk full"


def test_poll_handles_each_update_once_then_confirms(series, db):
    p = FakePipeline(series)
    p.notifier = QueueTG([{"update_id": 500, **_cb("x:3")}, {"update_id": 501, **_cb("a:4")}])
    w = Worker(p, db, "Asia/Kolkata", poll_timeout=0)
    db.kv_set("telegram_offset", "999999")  # a stale offset from the old scheme must not swallow taps
    assert w.poll_telegram() == 2
    assert p.calls == [("reject", 3), ("approve", 4)]
    assert p.notifier.calls == [None, 502] and p.notifier.queue == []
    assert w.poll_telegram() == 0 and p.calls == [("reject", 3), ("approve", 4)]


def test_crash_before_confirm_never_replays_a_tap(series, db):
    p = FakePipeline(series)
    p.notifier = QueueTG([{"update_id": 7, **_cb("x:3")}])
    w = Worker(p, db, "Asia/Kolkata", poll_timeout=0)
    real = p.notifier.get_updates

    def confirm_crashes(offset, timeout=50, limit=100):
        if offset is not None:
            raise ConnectionError("runner died")
        return real(offset, timeout, limit)

    p.notifier.get_updates = confirm_crashes
    with pytest.raises(ConnectionError):
        w.poll_telegram()
    p.notifier.get_updates = real
    assert w.poll_telegram() == 0  # still pending on Telegram's side, but already handled here
    assert p.calls == [("reject", 3)] and p.notifier.queue == []


def test_value_errors_are_reported_to_the_button(series, db):
    p = FakePipeline(series)

    def boom(job_id, when):
        raise ValueError("job 3 is published")

    p.approve = boom
    Worker(p, db, "Asia/Kolkata").handle_update(_cb("a:3"))
    assert p.notifier.answers == ["job 3 is published"]


class OrderPipeline(FakePipeline):
    def __init__(self, series, log):
        super().__init__(series)
        self.log = log

    def publish_due(self, now=None):
        self.log.append("publish")
        return []


def test_run_once_order_resume_poll_generate_publish(series, db, monkeypatch):
    log = []
    p = OrderPipeline(series, log)
    w = Worker(p, db, "Asia/Kolkata", poll_timeout=0)
    monkeypatch.setattr(w, "resume_interrupted", lambda: log.append("resume"))
    monkeypatch.setattr(w, "poll_telegram", lambda: log.append("poll"))
    monkeypatch.setattr(w, "maybe_generate", lambda: log.append("generate"))
    w.run_once()
    assert log == ["resume", "poll", "generate", "publish"]


def test_run_once_summary_says_what_happened(series, db, monkeypatch):
    p = OrderPipeline(series, [])
    p.notifier = QueueTG([{"update_id": 1, **_cb("x:3")}])
    w = Worker(p, db, "Asia/Kolkata", poll_timeout=0)
    monkeypatch.setattr(w, "now", lambda: datetime(2026, 10, 29, 11, 0, tzinfo=IST))  # a Thursday
    summary = w.run_once()
    assert summary.startswith("Telegram: 1 handled")
    assert "generate: not due" in summary and "publish: nothing due" in summary


def test_telegram_outage_does_not_block_generation_or_publishing(series, db, monkeypatch):
    log = []
    p = OrderPipeline(series, log)

    def down(*a, **k):
        raise ConnectionError("api.telegram.org unreachable")

    p.notifier.get_updates = down
    w = Worker(p, db, "Asia/Kolkata", poll_timeout=0)
    monkeypatch.setattr(w, "maybe_generate", lambda: log.append("generate"))
    assert "poll failed (ConnectionError)" in w.run_once()
    assert log == ["generate", "publish"]


class CastPipeline(FakePipeline):
    def __init__(self, series, missing):
        super().__init__(series)
        self.missing = missing
        self.planned = 0

    def cast_missing(self):
        return self.missing

    def lock_cast(self, name, idx, reply_to=None):
        self.calls.append(("cast", name, idx))
        self.missing = [m for m in self.missing if m != name]
        self.notifier.send_message("🎭 locked." + ("" if self.missing else " Cast complete"))

    def propose_cast(self, names=None):
        self.calls.append(("propose", names))

    def plan_next(self):
        self.planned += 1
        return 1

    def generate(self, job_id):
        self.calls.append(("generate", job_id))


def test_generation_waits_for_cast_and_nags_once_a_day(series, db, monkeypatch):
    p = CastPipeline(series, ["Kai"])
    w = Worker(p, db, "Asia/Kolkata")
    wed_11 = datetime(2026, 10, 28, 11, 0, tzinfo=IST)
    monkeypatch.setattr(w, "now", lambda: wed_11)
    assert w.maybe_generate() is None and w.maybe_generate() is None
    assert p.planned == 0 and db.kv_get("last_generation_date") is None  # the day is not burned
    assert sum("waiting for the cast" in m for m in p.notifier.messages) == 1
    p.missing = []
    assert w.maybe_generate() == 1 and ("generate", 1) in p.calls


def test_cast_buttons_and_command(series, db):
    p = CastPipeline(series, ["Kai"])
    w = Worker(p, db, "Asia/Kolkata")
    w.handle_update(_cb("c:Kai:2"))
    w.handle_update(_cb("cm:Kai"))
    w.handle_update({"message": {"text": "/cast kai", "chat": {"id": 42}}})
    w.handle_update({"message": {"text": "/cast", "chat": {"id": 666}}})  # unauthorized: ignored
    assert p.calls == [("cast", "Kai", 2), ("propose", ["Kai"]), ("propose", ["Kai"])]
    assert any("Cast complete" in m for m in p.notifier.messages)
