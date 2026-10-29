import io
import json

import httpx
import respx

from reelbot.inbox import R2Inbox
from reelbot.worker import Worker

from .test_worker import FakePipeline, FakeTG, _cb


class FakeS3:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})

    def list_objects_v2(self, Bucket, Prefix, **kw):
        return {"Contents": [{"Key": k} for k in sorted(self.objects) if k.startswith(Prefix)]}

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)


def _put(s3, update):
    s3.objects[f"inbox/{update['update_id']:012d}.json"] = json.dumps(update).encode()


def test_inbox_returns_updates_oldest_first_and_drops_junk():
    s3 = FakeS3({"inbox/bad.json": b"not json"})
    _put(s3, {"update_id": 12, **_cb("x:3")})
    _put(s3, {"update_id": 11, **_cb("a:3")})
    items = R2Inbox(s3, "state").pending()
    assert [u["update_id"] for _, u in items] == [11, 12]
    assert "inbox/bad.json" not in s3.objects


class NoPollTG(FakeTG):
    def get_updates(self, *a, **k):
        raise AssertionError("webhook mode must not call getUpdates")


def test_webhook_mode_handles_relayed_taps_once_and_clears_them(series, db):
    s3 = FakeS3()
    _put(s3, {"update_id": 5, **_cb("a:3")})
    p = FakePipeline(series)
    p.notifier = NoPollTG()
    w = Worker(p, db, "Asia/Kolkata", poll_timeout=0, inbox=R2Inbox(s3, "state"))
    assert w.poll_telegram() == 1
    assert p.calls == [("approve", 3)] and not s3.objects
    _put(s3, {"update_id": 5, **_cb("a:3")})  # Telegram redelivered it
    assert w.poll_telegram() == 0 and p.calls == [("approve", 3)] and not s3.objects


@respx.mock
def test_webhook_command_sets_the_relay_with_the_secret(monkeypatch, capsys):
    from reelbot.cli import main
    from reelbot.config import Settings

    s = Settings(
        _env_file=None, telegram_bot_token="T", telegram_chat_id="1", telegram_webhook_secret="S3CRET"
    )
    monkeypatch.setattr("reelbot.cli.get_settings", lambda: s)
    setw = respx.post("https://api.telegram.org/botT/setWebhook").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": True})
    )
    respx.post("https://api.telegram.org/botT/getWebhookInfo").mock(
        return_value=httpx.Response(
            200, json={"ok": True, "result": {"url": "https://relay.example", "pending_update_count": 0}}
        )
    )
    assert main(["webhook", "https://relay.example"]) == 0
    body = setw.calls[0].request.content.decode()
    assert "secret_token=S3CRET" in body and "callback_query" in body
    assert "url=https://relay.example" in capsys.readouterr().out
