from datetime import datetime
from zoneinfo import ZoneInfo

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

    def get_updates(self, offset, timeout=50):
        return []


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
