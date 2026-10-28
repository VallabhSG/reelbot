import httpx
import respx

from reelbot.telegram import Telegram, review_keyboard

API = "https://api.telegram.org/botT"


@respx.mock
def test_stale_callback_answer_is_swallowed():
    # cron hosts read taps up to 30 min late; Telegram then rejects the answer
    respx.post(f"{API}/answerCallbackQuery").mock(
        return_value=httpx.Response(
            400,
            json={"ok": False, "description": "Bad Request: query is too old and response timeout expired"},
        )
    )
    Telegram("T", "42").answer_callback("cb1", "Approved")  # must not raise


@respx.mock
def test_send_message_returns_id_and_targets_chat():
    route = respx.post(f"{API}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})
    )
    assert Telegram("T", "42").send_message("hi", reply_to=5) == 77
    body = route.calls[0].request.content.decode()
    assert "chat_id=42" in body and "reply_to_message_id=5" in body


def test_review_keyboard_has_a_button_per_shot():
    kb = review_keyboard(9, 3, music_regen=True)["inline_keyboard"]
    assert [b["callback_data"] for b in kb[1]] == ["s:9:0", "s:9:1", "s:9:2"]
    assert kb[2][0]["callback_data"] == "m:9"
