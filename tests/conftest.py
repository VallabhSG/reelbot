from __future__ import annotations

import shutil
from pathlib import Path

import pytest

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


@pytest.fixture
def db(tmp_path: Path) -> DB:
    return DB(tmp_path / "t.db")


@pytest.fixture
def dry_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None, dry_run=True, data_dir=tmp_path / "data", series_file=ROOT / "series" / "example.yaml"
    )
