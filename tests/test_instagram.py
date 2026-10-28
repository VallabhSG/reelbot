from urllib.parse import parse_qs

import httpx
import pytest
import respx

from reelbot.instagram import TOKEN_KEY, InstagramError, InstagramPublisher

G = "https://graph.instagram.com"


def pub(db, **kw):
    return InstagramPublisher("17841", "envtoken", db, poll_interval=0, poll_timeout=5, **kw)


@respx.mock
def test_publish_reel_happy_path(db):
    create = respx.post(f"{G}/17841/media").mock(return_value=httpx.Response(200, json={"id": "C1"}))
    respx.get(f"{G}/C1").mock(
        side_effect=[
            httpx.Response(200, json={"status_code": "IN_PROGRESS"}),
            httpx.Response(200, json={"status_code": "FINISHED"}),
        ]
    )
    publish = respx.post(f"{G}/17841/media_publish").mock(return_value=httpx.Response(200, json={"id": "M1"}))

    caption = "NIGHT SHIFT\nNight 1 / 100\n\nhook line\n\n#aianimation"
    assert pub(db).publish_reel("https://cdn/r.mp4", caption, 1200) == "M1"

    body = parse_qs(create.calls[0].request.content.decode())
    assert body["media_type"] == ["REELS"] and body["video_url"] == ["https://cdn/r.mp4"]
    assert body["caption"] == [caption] and body["thumb_offset"] == ["1200"]
    assert "caption" not in str(create.calls[0].request.url)  # caption sent in the body, not the URL
    assert parse_qs(publish.calls[0].request.content.decode())["creation_id"] == ["C1"]
    assert create.calls[0].request.url.params["access_token"] == "envtoken"


@respx.mock
def test_versioned_base_url(db):
    route = respx.post(f"{G}/v23.0/17841/media").mock(return_value=httpx.Response(200, json={"id": "C"}))
    pub(db, version="v23.0").create_reel_container("u", "c", None)
    assert route.called


@respx.mock
def test_container_error_raises(db):
    respx.post(f"{G}/17841/media").mock(return_value=httpx.Response(200, json={"id": "C2"}))
    respx.get(f"{G}/C2").mock(
        return_value=httpx.Response(200, json={"status_code": "ERROR", "status": "bad codec"})
    )
    with pytest.raises(InstagramError, match="ERROR"):
        pub(db).publish_reel("u", "c")


@respx.mock
def test_client_error_is_not_retried(db):
    route = respx.post(f"{G}/17841/media").mock(
        return_value=httpx.Response(400, json={"error": {"message": "x"}})
    )
    with pytest.raises(InstagramError, match="400"):
        pub(db).create_reel_container("u", "c", None)
    assert len(route.calls) == 1


@respx.mock
def test_token_refresh_is_stored_and_used(db):
    respx.get(f"{G}/refresh_access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "newtoken", "expires_in": 5184000})
    )
    p = pub(db)
    assert p.refresh_token_if_due() is True
    assert db.kv_get(TOKEN_KEY) == "newtoken" and p.token == "newtoken"
    assert p.refresh_token_if_due() is False  # not due again within a week


@respx.mock
def test_refresh_of_fresh_token_fails_soft(db):
    # Instagram refuses to refresh tokens younger than 24h; that must not block the first post
    respx.get(f"{G}/refresh_access_token").mock(
        return_value=httpx.Response(400, json={"error": {"message": "token too new"}})
    )
    p = pub(db)
    assert p.refresh_token_if_due() is False
    assert p.token == "envtoken"
    assert db.kv_get(TOKEN_KEY) is None
