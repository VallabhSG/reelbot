"""Minimal Telegram Bot API client for the review loop (long polling, no webhook server needed)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    pass


def review_keyboard(job_id: int, n_shots: int, music_regen: bool) -> dict[str, Any]:
    rows = [
        [
            {"text": "✅ Approve", "callback_data": f"a:{job_id}"},
            {"text": "❌ Reject", "callback_data": f"x:{job_id}"},
        ],
        [{"text": f"🔁 Shot {i + 1}", "callback_data": f"s:{job_id}:{i}"} for i in range(n_shots)],
    ]
    if music_regen:
        rows.append([{"text": "🎵 New music", "callback_data": f"m:{job_id}"}])
    return {"inline_keyboard": rows}


class Telegram:
    def __init__(self, token: str, chat_id: str, http: httpx.Client | None = None):
        if not token or not chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = str(chat_id)
        self.http = http or httpx.Client(timeout=httpx.Timeout(70.0, connect=15.0))

    def _call(self, method: str, files: dict | None = None, **params: Any) -> Any:
        data = {
            k: (json.dumps(v) if isinstance(v, dict | list) else v)
            for k, v in params.items()
            if v is not None
        }
        r = self.http.post(f"{self.base}/{method}", data=data, files=files)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if not body.get("ok"):
            raise TelegramError(f"{method} failed: {r.status_code} {body.get('description', r.text[:200])}")
        return body["result"]

    def send_message(self, text: str, reply_markup: dict | None = None, reply_to: int | None = None) -> int:
        return self._call(
            "sendMessage",
            chat_id=self.chat_id,
            text=text[:4096],
            reply_markup=reply_markup,
            reply_to_message_id=reply_to,
        )["message_id"]

    def send_video(self, path: Path, caption: str, reply_markup: dict | None = None) -> int:
        with path.open("rb") as f:
            return self._call(
                "sendVideo",
                files={"video": (path.name, f, "video/mp4")},
                chat_id=self.chat_id,
                caption=caption[:1024],
                supports_streaming="true",
                reply_markup=reply_markup,
            )["message_id"]

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self._call("answerCallbackQuery", callback_query_id=callback_id, text=text[:200])

    def clear_keyboard(self, message_id: int) -> None:
        try:
            self._call(
                "editMessageReplyMarkup",
                chat_id=self.chat_id,
                message_id=message_id,
                reply_markup={"inline_keyboard": []},
            )
        except TelegramError as e:  # already edited / too old: harmless
            log.debug("clear_keyboard: %s", e)

    def get_updates(self, offset: int | None, timeout: int = 50) -> list[dict[str, Any]]:
        return self._call(
            "getUpdates", offset=offset, timeout=timeout, allowed_updates=["message", "callback_query"]
        )


class ConsoleNotifier:
    """Dry-run stand-in: logs what would be sent; review via `reelbot approve|reject|regen`."""

    chat_id = "console"

    def __init__(self) -> None:
        self.sent: list[str] = []
        self._next = 1

    def send_message(self, text: str, reply_markup: dict | None = None, reply_to: int | None = None) -> int:
        self.sent.append(text)
        log.info("[notify] %s", text)
        self._next += 1
        return self._next

    def send_video(self, path: Path, caption: str, reply_markup: dict | None = None) -> int:
        self.sent.append(f"VIDEO {path}\n{caption}")
        log.info("[preview] %s\n%s", path, caption)
        self._next += 1
        return self._next

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        pass

    def clear_keyboard(self, message_id: int) -> None:
        pass

    def get_updates(self, offset: int | None, timeout: int = 50) -> list[dict[str, Any]]:
        return []
