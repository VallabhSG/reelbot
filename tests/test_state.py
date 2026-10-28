from pathlib import Path

import pytest

from reelbot.config import Settings
from reelbot.db import DB
from reelbot.models import EpisodePlan, JobStatus
from reelbot.state import StateSync


class FakeS3:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):  # noqa: N803
        return {
            "Contents": [{"Key": k} for k in sorted(self.objects) if k.startswith(Prefix)],
            "IsTruncated": False,
        }

    def download_file(self, bucket, key, dest):
        Path(dest).write_bytes(self.objects[key])

    def upload_file(self, src, bucket, key):
        self.objects[key] = Path(src).read_bytes()

    def delete_objects(self, Bucket, Delete):  # noqa: N803
        for o in Delete["Objects"]:
            self.objects.pop(o["Key"], None)


def _settings(tmp_path: Path, name: str) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path / name, s3_bucket="media", s3_state_bucket="state")


def _plan() -> EpisodePlan:
    return EpisodePlan(
        title="T",
        category="c",
        logline="l",
        hook="h",
        music_prompt="m",
        shots=[{"keyframe_prompt": "a long enough prompt", "motion_prompt": "slow pan"}],
    )


def test_roundtrip_between_two_ephemeral_runners(tmp_path):
    s3 = FakeS3()
    a = _settings(tmp_path, "runner_a")
    db = DB(a.db_path)
    live = db.create_job(1, _plan())
    done = db.create_job(2, _plan())
    db.update_job(live, status=JobStatus.AWAITING_REVIEW)
    db.update_job(done, status=JobStatus.PUBLISHED)
    db.kv_set("ig_access_token", "refreshed")
    for jid in (live, done):
        (a.jobs_dir / str(jid)).mkdir(parents=True)
        (a.jobs_dir / str(jid) / "final.mp4").write_bytes(b"video%d" % jid)
    s3.objects[f"state/jobs/{done}/final.mp4"] = b"old"  # left over from an earlier run
    StateSync(a, client=s3).push()

    assert f"state/jobs/{live}/final.mp4" in s3.objects
    assert f"state/jobs/{done}/final.mp4" not in s3.objects  # finished jobs are pruned

    b = _settings(tmp_path, "runner_b")
    assert StateSync(b, client=s3).pull() is True
    db_b = DB(b.db_path)
    assert db_b.get_job(live)["status"] == JobStatus.AWAITING_REVIEW
    assert db_b.kv_get("ig_access_token") == "refreshed"
    assert (b.jobs_dir / str(live) / "final.mp4").read_bytes() == b"video%d" % live
    assert not (b.jobs_dir / str(done)).exists()


def test_pull_on_empty_bucket_starts_fresh(tmp_path):
    assert StateSync(_settings(tmp_path, "x"), client=FakeS3()).pull() is False


def test_state_bucket_must_differ_from_public_bucket(tmp_path):
    s = Settings(_env_file=None, data_dir=tmp_path, s3_bucket="same", s3_state_bucket="same")
    with pytest.raises(ValueError, match="private"):
        StateSync(s, client=FakeS3())
