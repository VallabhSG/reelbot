from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from reelbot.config import Settings
from reelbot.db import DB
from reelbot.series import Series, load_series

ROOT = Path(__file__).resolve().parents[1]

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


@pytest.fixture
def series() -> Series:
    s = load_series(ROOT / "series" / "example.yaml")
    s.base_dir = ROOT
    return s


TAKE_MODELS = {
    "kling3": {
        "label": "Kling 3.0",
        "endpoint": "kling-video/v3.0/pro/image-to-video",
        "input": {"prompt": "{prompt}", "image_url": "{start_image}", "duration": "{duration}"},
        "output_path": "video.url",
        "cost_per_second": 0.12,
        "ref_token": "",
        "start_frame": True,
    },
    "seedance2": {
        "label": "Seedance 2.0",
        "endpoint": "bytedance/seedance-2.0/reference-to-video",
        "input": {"prompt": "{prompt}", "image_urls": "{image_urls}", "duration": "{duration}"},
        "output_path": "video.url",
        "cost_per_second": 0.25,
        "ref_token": "image {n}",
        "start_frame": False,
    },
}


def make_take_series(**take) -> Series:
    """The example series switched to v3 one-take mode with both bake-off models."""
    raw = yaml.safe_load((ROOT / "series" / "example.yaml").read_text(encoding="utf-8"))
    raw["take"] = {
        "enabled": True,
        "seconds": 15,
        "shots": 6,
        "model": "kling3",
        "bakeoff": ["kling3", "seedance2"],
        **take,
    }
    raw["models"]["takes"] = TAKE_MODELS
    raw["base_dir"] = ROOT
    return Series.model_validate(raw)


@pytest.fixture
def take_series() -> Series:
    return make_take_series()


@pytest.fixture
def db(tmp_path: Path) -> DB:
    return DB(tmp_path / "t.db")


@pytest.fixture
def dry_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None, dry_run=True, data_dir=tmp_path / "data", series_file=ROOT / "series" / "example.yaml"
    )
