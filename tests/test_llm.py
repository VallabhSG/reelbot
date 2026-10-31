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


@respx.mock
def test_gpt_oss_thinks_lightly_and_other_models_get_no_reasoning_field():
    import json as _json

    ok = {"choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}]}
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=ok))
    LLMClient("https://llm.test/v1", "k", "openai/gpt-oss-120b", reasoning_effort="low").json_chat("s", "u")
    LLMClient("https://llm.test/v1", "k", "llama-x", reasoning_effort="low").json_chat("s", "u")
    bodies = [_json.loads(c.request.content) for c in route.calls]
    assert bodies[0]["reasoning_effort"] == "low" and "reasoning_effort" not in bodies[1]


@respx.mock
def test_a_cut_off_answer_is_named_as_such():
    import pytest

    from reelbot.providers.llm import AnswerCutOff

    cut = {"choices": [{"message": {"content": '{"title": "X", "sho'}, "finish_reason": "length"}]}
    broken = {"error": {"code": "json_validate_failed", "message": "Failed", "failed_generation": "{..."}}
    respx.post(URL).mock(side_effect=[httpx.Response(200, json=cut), httpx.Response(400, json=broken)])
    client = LLMClient("https://llm.test/v1", "k", "m")
    with pytest.raises(AnswerCutOff, match="output limit"):
        client.json_chat("s", "u")
    with pytest.raises(AnswerCutOff, match="incomplete"):
        client.json_chat("s", "u")


@respx.mock
def test_other_client_errors_say_what_the_provider_said():
    import pytest

    bad = {"error": {"code": "model_not_found", "message": "The model does not exist"}}
    respx.post(URL).mock(return_value=httpx.Response(404, json=bad))
    with pytest.raises(httpx.HTTPStatusError, match="The model does not exist"):
        LLMClient("https://llm.test/v1", "k", "m").json_chat("s", "u")
