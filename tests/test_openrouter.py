from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
import respx
from tenacity import wait_none

from reelbot.budget import BudgetGuard
from reelbot.providers.higgsfield import HiggsfieldClient, HiggsfieldMedia
from reelbot.providers.openrouter_video import BASE_URL, OpenRouterError, OpenRouterVideo
from reelbot.series import Budget, load_series

ROOT = Path(__file__).resolve().parents[1]
MODEL = "bytedance/seedance-2.0"


def orv(tmp_path, **kw):
    return OpenRouterVideo("OR-KEY", poll_interval=0, timeout_s=5, work_dir=tmp_path / "or", **kw)


def _submit_ok():
    return respx.post(f"{BASE_URL}/videos").mock(
        return_value=httpx.Response(
            202, json={"id": "J1", "polling_url": f"{BASE_URL}/videos/J1", "status": "pending"}
        )
    )


@respx.mock
def test_submit_poll_download_with_the_key_on_openrouter_only(tmp_path):
    submit = _submit_ok()
    respx.get(f"{BASE_URL}/videos/J1").mock(
        side_effect=[
            httpx.Response(200, json={"id": "J1", "status": "pending"}),
            httpx.Response(200, json={"id": "J1", "status": "in_progress"}),
            httpx.Response(
                200,
                json={
                    "id": "J1",
                    "status": "completed",
                    "unsigned_urls": [f"{BASE_URL}/videos/J1/content?index=0"],
                    "usage": {"cost": 2.268, "is_byok": False},
                },
            ),
        ]
    )
    content = respx.get(f"{BASE_URL}/videos/J1/content").mock(
        return_value=httpx.Response(200, content=b"mp4bytes")
    )
    url, cost = orv(tmp_path).generate(MODEL, {"prompt": "p", "duration": 15})
    assert cost == pytest.approx(2.268)
    assert Path(urlparse(url).path).read_bytes() == b"mp4bytes"
    body = submit.calls[0].request
    assert body.headers["Authorization"] == "Bearer OR-KEY"
    assert b'"model":"bytedance/seedance-2.0"' in body.content.replace(b" ", b"")
    assert content.calls[0].request.headers["Authorization"] == "Bearer OR-KEY"


@respx.mock
def test_signed_urls_are_fetched_without_the_key_and_foreign_hosts_never_get_it(tmp_path):
    _submit_ok()
    respx.get(f"{BASE_URL}/videos/J1").mock(
        return_value=httpx.Response(
            200, json={"status": "completed", "urls": ["https://cdn.example/v.mp4?sig=x"]}
        )
    )
    signed = respx.get("https://cdn.example/v.mp4").mock(return_value=httpx.Response(200, content=b"v"))
    url, cost = orv(tmp_path).generate(MODEL, {"prompt": "p"})
    assert "Authorization" not in signed.calls[0].request.headers and cost is None

    respx.get(f"{BASE_URL}/videos/J1").mock(
        return_value=httpx.Response(
            200, json={"status": "completed", "unsigned_urls": ["https://evil.example/c"]}
        )
    )
    own = respx.get(f"{BASE_URL}/videos/J1/content").mock(return_value=httpx.Response(200, content=b"v"))
    evil = respx.get("https://evil.example/c").mock(return_value=httpx.Response(200, content=b"x"))
    orv(tmp_path).generate(MODEL, {"prompt": "p"})
    assert own.called and not evil.called


@respx.mock
def test_failed_job_reports_openrouters_reason(tmp_path):
    _submit_ok()
    respx.get(f"{BASE_URL}/videos/J1").mock(
        return_value=httpx.Response(
            200, json={"status": "failed", "error": "content policy: first frame rejected"}
        )
    )
    with pytest.raises(OpenRouterError, match="content policy: first frame rejected"):
        orv(tmp_path).generate(MODEL, {"prompt": "p"})


@respx.mock
def test_rejected_request_is_not_retried(tmp_path):
    route = respx.post(f"{BASE_URL}/videos").mock(
        return_value=httpx.Response(400, json={"error": {"message": "duration must be 4-15"}})
    )
    with pytest.raises(OpenRouterError, match="duration must be 4-15"):
        orv(tmp_path).generate(MODEL, {"prompt": "p", "duration": 20})
    assert route.call_count == 1


@respx.mock
def test_a_timeout_after_sending_is_not_resubmitted(tmp_path):
    """It may have started a (billed) job: a second submit would be a second charge."""
    route = respx.post(f"{BASE_URL}/videos").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(httpx.ReadTimeout):
        orv(tmp_path).generate(MODEL, {"prompt": "p"})
    assert route.call_count == 1


@respx.mock
def test_a_refused_connection_is_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(OpenRouterVideo.submit.retry, "wait", wait_none())
    route = respx.post(f"{BASE_URL}/videos").mock(
        side_effect=[
            httpx.ConnectError("down"),
            httpx.Response(202, json={"id": "J1", "status": "pending"}),
        ]
    )
    respx.get(f"{BASE_URL}/videos/J1").mock(
        return_value=httpx.Response(200, json={"status": "completed", "urls": ["https://cdn.example/v"]})
    )
    respx.get("https://cdn.example/v").mock(return_value=httpx.Response(200, content=b"v"))
    orv(tmp_path).generate(MODEL, {"prompt": "p"})
    assert route.call_count == 2


def test_the_series_take_goes_to_openrouter_with_the_opening_frame(tmp_path, db):
    series = load_series(ROOT / "series" / "everyday-boss-fights.yaml")
    assert series.uses_provider("openrouter") and series.models.takes["seedance2"].provider == "openrouter"

    class FakeOR:
        def __init__(self):
            self.calls = []

        def generate(self, model, body):
            self.calls.append((model, body))
            return "file:///tmp/take.mp4", 2.27

    fake = FakeOR()
    guard = BudgetGuard(db, Budget(monthly_usd=40, per_reel_usd=9), "Asia/Kolkata")
    hf = HiggsfieldClient("id", "secret", "https://api.higgsfield.ai")
    media = HiggsfieldMedia(hf, series.models, guard, series.base_dir, openrouter=fake)
    ctx = {
        "prompt": "Juno vs the closing door",
        "start_image": "https://cdn.higgsfield.ai/frame.png",
        "duration": 15,
        "aspect_ratio": "9:16",
    }
    assert media.take("seedance2", ctx, job_id=7) == "file:///tmp/take.mp4"
    model, body = fake.calls[0]
    assert model == "bytedance/seedance-2.0"
    assert body["duration"] == 15 and body["resolution"] == "720p" and body["aspect_ratio"] == "9:16"
    assert body["generate_audio"] is True
    assert body["frame_images"] == [
        {
            "type": "image_url",
            "image_url": {"url": "https://cdn.higgsfield.ai/frame.png"},
            "frame_type": "first_frame",
        }
    ]
    assert db.spend_for_job(7) == pytest.approx(2.27)  # the real cost OpenRouter reported
    assert series.estimate_reel_cost() < 3  # was ~$4.75 on Higgsfield


def test_without_a_key_an_openrouter_take_says_what_to_set(db):
    series = load_series(ROOT / "series" / "everyday-boss-fights.yaml")
    guard = BudgetGuard(db, Budget(monthly_usd=40, per_reel_usd=9), "Asia/Kolkata")
    hf = HiggsfieldClient("id", "secret", "https://api.higgsfield.ai")
    media = HiggsfieldMedia(hf, series.models, guard, series.base_dir)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        media.take(
            "seedance2", {"prompt": "p", "start_image": "u", "duration": 15, "aspect_ratio": "9:16"}, 1
        )


def test_example_series_does_not_need_openrouter(series):
    assert not series.uses_provider("openrouter")
