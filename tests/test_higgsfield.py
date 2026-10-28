import httpx
import pytest
import respx

from reelbot.budget import BudgetExceeded, BudgetGuard
from reelbot.providers.higgsfield import HiggsfieldClient, HiggsfieldError, HiggsfieldMedia
from reelbot.series import Budget

BASE = "https://api.higgsfield.ai"


def client():
    return HiggsfieldClient("id", "secret", BASE, poll_interval=0, timeout_s=5)


@respx.mock
def test_submit_poll_complete_and_auth_headers():
    submit = respx.post(f"{BASE}/higgsfield-ai/soul/v2/standard").mock(
        return_value=httpx.Response(
            200, json={"status": "queued", "request_id": "r1", "status_url": f"{BASE}/requests/r1/status"}
        )
    )
    respx.get(f"{BASE}/requests/r1/status").mock(
        side_effect=[
            httpx.Response(200, json={"status": "in_progress", "request_id": "r1"}),
            httpx.Response(
                200, json={"status": "completed", "request_id": "r1", "images": [{"url": "https://x/i.png"}]}
            ),
        ]
    )
    out = client().run("higgsfield-ai/soul/v2/standard", {"prompt": "p"})
    assert out["images"][0]["url"] == "https://x/i.png"
    req = submit.calls[0].request
    assert req.headers["Authorization"] == "Key id:secret"
    assert req.headers["Idempotency-Key"]


@respx.mock
def test_retry_reuses_idempotency_key():
    route = respx.post(f"{BASE}/m").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json={"status": "queued", "request_id": "r2"}),
        ]
    )
    c = client()
    c._post.retry.wait = lambda *_: 0  # no sleeping in tests
    c.submit("m", {})
    keys = {call.request.headers["Idempotency-Key"] for call in route.calls}
    assert len(route.calls) == 2 and len(keys) == 1


@respx.mock
def test_4xx_is_not_retried():
    route = respx.post(f"{BASE}/m").mock(
        return_value=httpx.Response(422, json={"detail": "image_urls: field required"})
    )
    with pytest.raises(HiggsfieldError) as e:
        client().submit("m", {})
    assert len(route.calls) == 1
    # the API's own explanation reaches Telegram, so a wrong field name is a one-line fix
    assert "HTTP 422" in str(e.value) and "image_urls: field required" in str(e.value)


@respx.mock
@pytest.mark.parametrize("status", ["failed", "nsfw", "canceled"])
def test_bad_terminal_status_raises(status):
    respx.post(f"{BASE}/m").mock(
        return_value=httpx.Response(200, json={"status": "queued", "request_id": "r"})
    )
    respx.get(f"{BASE}/requests/r/status").mock(return_value=httpx.Response(200, json={"status": status}))
    with pytest.raises(HiggsfieldError) as e:
        client().run("m", {})
    assert e.value.status == status
    assert "(no reason given)" in str(e.value)


@respx.mock
def test_failure_reason_reaches_the_error():
    respx.post(f"{BASE}/m").mock(
        return_value=httpx.Response(200, json={"status": "queued", "request_id": "r"})
    )
    respx.get(f"{BASE}/requests/r/status").mock(
        return_value=httpx.Response(
            200, json={"status": "failed", "request_id": "r", "error": "content policy: reference image"}
        )
    )
    with pytest.raises(HiggsfieldError, match="content policy: reference image"):
        client().run("m", {})


@respx.mock
def test_media_charges_budget_only_on_success(series, db):
    guard = BudgetGuard(db, Budget(monthly_usd=10, per_reel_usd=10), "Asia/Kolkata")
    series.models.video.endpoint = "kling/i2v"
    media = HiggsfieldMedia(client(), series.models, guard, series.base_dir)
    respx.post(f"{BASE}/kling/i2v").mock(
        return_value=httpx.Response(200, json={"status": "queued", "request_id": "v"})
    )
    respx.get(f"{BASE}/requests/v/status").mock(
        side_effect=[
            httpx.Response(200, json={"status": "failed"}),
            httpx.Response(200, json={"status": "completed", "video": {"url": "https://x/v.mp4"}}),
        ]
    )
    ctx = {"prompt": "p", "image_urls": ["https://x/k.png"], "duration": 5, "aspect_ratio": "9:16"}
    with pytest.raises(HiggsfieldError):
        media.video(ctx, job_id=1)
    assert db.spend_for_job(1) == 0
    assert media.video(ctx, job_id=1) == "https://x/v.mp4"
    assert db.spend_for_job(1) == pytest.approx(5 * series.models.video.cost_per_second)


def test_media_refuses_call_over_budget(series, db):
    guard = BudgetGuard(db, Budget(monthly_usd=0.01, per_reel_usd=10), "Asia/Kolkata")
    media = HiggsfieldMedia(client(), series.models, guard, series.base_dir)
    with pytest.raises(BudgetExceeded):  # no HTTP mock: proves no request is attempted
        media.video({"prompt": "p", "image_urls": ["u"], "duration": 5, "aspect_ratio": "9:16"}, job_id=1)


@respx.mock
def test_upload_file_uses_presigned_url_without_credentials(tmp_path):
    f = tmp_path / "ref.png"
    f.write_bytes(b"\x89PNG")
    respx.post(f"{BASE}/files/generate-upload-url").mock(
        return_value=httpx.Response(
            200,
            json={
                "public_url": "https://cdn/ref.png",
                "upload_url": "https://s3/put",
                "upload_headers": {"Content-Type": "image/png", "x-amz-tagging": "t"},
            },
        )
    )
    put = respx.put("https://s3/put").mock(return_value=httpx.Response(200))
    assert client().upload_file(f) == "https://cdn/ref.png"
    assert "Authorization" not in put.calls[0].request.headers
    assert put.calls[0].request.headers["x-amz-tagging"] == "t"
