import httpx
import respx

from reelbot.providers.llm import LLMClient

URL = "https://llm.test/v1/chat/completions"


@respx.mock
def test_rate_limit_waits_as_told_then_succeeds():
    ok = {"choices": [{"message": {"content": '{"ok": true}'}}]}
    route = respx.post(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "0.01"}, json={"error": "tpm"}),
            httpx.Response(429, headers={"retry-after": "0.01"}, json={"error": "tpm"}),
            httpx.Response(200, json=ok),
        ]
    )
    assert LLMClient("https://llm.test/v1", "k", "m").json_chat("s", "u") == {"ok": True}
    assert route.call_count == 3
