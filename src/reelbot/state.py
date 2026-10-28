"""Persist worker state between ephemeral runs (GitHub Actions) in a PRIVATE S3/R2 bucket.

pull: state/reelbot.db + state/jobs/<id>/* for jobs still in flight
push: same, then delete remote files of finished (published/rejected) jobs
The DB holds the refreshed Instagram token, so this bucket must never be public.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta

from .config import Settings
from .db import DB
from .models import JobStatus

log = logging.getLogger(__name__)

PREFIX = "state"
IN_FLIGHT = (
    JobStatus.PLANNED,
    JobStatus.GENERATING,
    JobStatus.AWAITING_REVIEW,
    JobStatus.APPROVED,
    JobStatus.PUBLISHING,
)
DONE = (JobStatus.PUBLISHED, JobStatus.REJECTED)
FAILED_KEEP = timedelta(days=7)  # failed jobs stay resumable for a week


class StateSync:
    def __init__(self, s: Settings, client=None):
        if not s.s3_state_bucket:
            raise ValueError("S3_STATE_BUCKET is required for state sync")
        if s.s3_state_bucket == s.s3_bucket:
            raise ValueError("S3_STATE_BUCKET must be a separate, private bucket (it stores tokens)")
        if client is None:
            import boto3

            client = boto3.client(
                "s3",
                endpoint_url=s.s3_endpoint_url,
                aws_access_key_id=s.s3_access_key_id,
                aws_secret_access_key=s.s3_secret_access_key,
                region_name="auto",
            )
        self.s3 = client
        self.bucket = s.s3_state_bucket
        self.data_dir = s.data_dir
        self.db_path = s.db_path
        self.jobs_dir = s.jobs_dir

    def _keys(self, prefix: str) -> list[str]:
        keys, token = [], None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            page = self.s3.list_objects_v2(**kw)
            keys += [o["Key"] for o in page.get("Contents", [])]
            if not page.get("IsTruncated"):
                return keys
            token = page["NextContinuationToken"]

    def _job_ids(self, db: DB, statuses: tuple[JobStatus, ...]) -> list[int]:
        return [j["id"] for j in db.jobs_with_status(*statuses)]

    def _resumable_failed(self, db: DB) -> list[int]:
        cutoff = (datetime.now(UTC) - FAILED_KEEP).isoformat(timespec="seconds")
        return [j["id"] for j in db.jobs_with_status(JobStatus.FAILED) if j["updated_at"] >= cutoff]

    def pull(self) -> bool:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self._keys(f"{PREFIX}/reelbot.db"):
            log.info("no remote state yet; starting fresh")
            return False
        self.s3.download_file(self.bucket, f"{PREFIX}/reelbot.db", str(self.db_path))
        db = DB(self.db_path)
        n = 0
        for job_id in self._job_ids(db, IN_FLIGHT) + self._resumable_failed(db):
            for key in self._keys(f"{PREFIX}/jobs/{job_id}/"):
                dest = self.data_dir / key.removeprefix(f"{PREFIX}/")
                dest.parent.mkdir(parents=True, exist_ok=True)
                self.s3.download_file(self.bucket, key, str(dest))
                n += 1
        log.info("pulled state: db + %d job files", n)
        return True

    def push(self) -> None:
        if not self.db_path.exists():
            log.info("no local db; nothing to push")
            return
        with sqlite3.connect(self.db_path) as conn:  # fold the WAL into the main file before upload
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.s3.upload_file(str(self.db_path), self.bucket, f"{PREFIX}/reelbot.db")
        db = DB(self.db_path)
        n = 0
        for job_id in self._job_ids(db, IN_FLIGHT) + self._resumable_failed(db):
            job_dir = self.jobs_dir / str(job_id)
            for f in sorted(job_dir.glob("*")) if job_dir.exists() else []:
                if f.is_file() and not f.name.endswith(".part"):
                    self.s3.upload_file(str(f), self.bucket, f"{PREFIX}/jobs/{job_id}/{f.name}")
                    n += 1
        stale = [k for jid in self._job_ids(db, DONE) for k in self._keys(f"{PREFIX}/jobs/{jid}/")]
        for i in range(0, len(stale), 1000):
            self.s3.delete_objects(
                Bucket=self.bucket, Delete={"Objects": [{"Key": k} for k in stale[i : i + 1000]]}
            )
        log.info("pushed state: db + %d job files, removed %d finished-job files", n, len(stale))
