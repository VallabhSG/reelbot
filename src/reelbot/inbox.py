"""Webhook mode: Telegram taps arrive instantly at a tiny Cloudflare Worker (relay/telegram-relay.js),
which drops each update into the private state bucket under inbox/ and starts a GitHub Actions run.
The run reads the inbox here instead of calling getUpdates (which Telegram blocks while a webhook is
set). Updates wait safely in the bucket, so a run that GitHub cancels or delays loses nothing."""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

PREFIX = "inbox/"


class R2Inbox:
    def __init__(self, s3: Any, bucket: str):
        self.s3 = s3
        self.bucket = bucket

    @classmethod
    def from_settings(cls, settings: Any) -> R2Inbox:
        from .state import StateSync

        sync = StateSync(settings)
        return cls(sync.s3, sync.bucket)

    def pending(self) -> list[tuple[str, dict[str, Any]]]:
        """(key, update) oldest first."""
        keys, token = [], None
        while True:
            kw: dict[str, Any] = {"Bucket": self.bucket, "Prefix": PREFIX}
            if token:
                kw["ContinuationToken"] = token
            page = self.s3.list_objects_v2(**kw)
            keys += [o["Key"] for o in page.get("Contents", [])]
            if not page.get("IsTruncated"):
                break
            token = page["NextContinuationToken"]
        out = []
        for key in keys:
            try:
                body = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
                update = json.loads(body)
                int(update["update_id"])
            except Exception as e:  # unreadable: drop it rather than retry it forever
                log.warning("inbox: dropping unreadable %s (%s)", key, e)
                self.done(key)
                continue
            out.append((key, update))
        return sorted(out, key=lambda kv: int(kv[1]["update_id"]))

    def done(self, key: str) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=key)
