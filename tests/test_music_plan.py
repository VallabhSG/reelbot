"""The score in sections timed to the story beats, and the provider that composes it."""

import httpx
import pytest
import respx

from reelbot.music_plan import MIN_MS, music_chunks
from reelbot.providers.music import ElevenLabsMusic
from reelbot.series import MusicSpec
from reelbot.storyboard import normalise

from .test_take import _plan


def test_one_section_per_beat_timed_to_the_shots_and_never_under_three_seconds(take_series):
    plan = normalise(take_series, _plan(seconds=(2, 2, 2, 2, 4, 3)))  # hook escalate clash clash climax twist
    chunks = music_chunks(take_series, plan, triumph=True)
    assert [c["text"].splitlines()[0] for c in chunks] == [
        "[Intro into Build]",
        "[Battle]",
        "[Climax]",
        "[Victory]",
    ]
    assert [c["duration_ms"] for c in chunks] == [4000, 4000, 4000, 3000]
    assert sum(c["duration_ms"] for c in chunks) == sum(s.seconds for s in plan.shots) * 1000
    assert all(c["duration_ms"] >= MIN_MS for c in chunks)
    assert "driving drum and bass" in chunks[0]["positive_styles"]  # the planner's own prompt
    assert f"{take_series.take.music_bpm} BPM" in chunks[1]["positive_styles"]
    assert "vocals" in chunks[0]["negative_styles"] and "{instrumental:" in chunks[0]["text"]


def test_a_deadpan_episode_ends_on_a_dead_stop_and_a_short_end_merges_back(take_series):
    plan = normalise(take_series, _plan(seconds=(3, 3, 3, 2, 2, 2)))
    chunks = music_chunks(take_series, plan, triumph=False)
    assert chunks[-1]["text"].startswith("[Climax into Deadpan]")
    assert "music stops dead" in chunks[-1]["positive_styles"]
    assert all(c["duration_ms"] >= MIN_MS for c in chunks)


class Guard:
    def check(self, cost, job_id):
        pass

    def record(self, *a):
        self.recorded = a


@respx.mock
def test_the_plan_is_composed_on_the_plan_model_with_a_tail(tmp_path, take_series):
    import json

    route = respx.post("https://api.elevenlabs.io/v1/music").mock(
        return_value=httpx.Response(200, content=b"ID3mp3")
    )
    music = ElevenLabsMusic("k", MusicSpec(), Guard())
    chunks = music_chunks(take_series, normalise(take_series, _plan()), triumph=True)
    out = music.track("lofi", 15, tmp_path / "m.mp3", 1, seed=7, chunks=chunks)
    body = json.loads(route.calls[0].request.content)
    assert out.read_bytes() == b"ID3mp3" and body["model_id"] == "music_v2_5" and body["seed"] == 7
    sent = body["composition_plan"]["chunks"]
    assert sent[-1]["duration_ms"] == chunks[-1]["duration_ms"] + 1500  # room for the fade-out


@respx.mock
def test_a_refused_plan_still_gets_a_track_from_the_prompt(tmp_path, take_series, monkeypatch):
    respx.post("https://api.elevenlabs.io/v1/music").mock(
        return_value=httpx.Response(403, json={"detail": "plan"})
    )
    music = ElevenLabsMusic("k", MusicSpec(), Guard())
    asked = {}

    def compose(**kw):
        asked.update(kw)
        return b"fallback"

    monkeypatch.setattr(music.client.music, "compose", compose)
    chunks = music_chunks(take_series, normalise(take_series, _plan()), triumph=True)
    out = music.track("lofi", 15, tmp_path / "m.mp3", 1, seed=7, chunks=chunks)
    assert out.read_bytes() == b"fallback" and asked["prompt"] == "lofi" and asked["force_instrumental"]


@pytest.mark.parametrize("composition", [False])
def test_composition_can_be_switched_off(tmp_path, take_series, monkeypatch, composition):
    music = ElevenLabsMusic("k", MusicSpec(composition=composition), Guard())
    monkeypatch.setattr(music.client.music, "compose", lambda **kw: b"prompt")
    chunks = music_chunks(take_series, normalise(take_series, _plan()), triumph=True)
    assert music.track("lofi", 15, tmp_path / "m.mp3", 1, seed=7, chunks=chunks).read_bytes() == b"prompt"
