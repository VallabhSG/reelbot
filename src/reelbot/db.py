"""SQLite state: jobs, per-shot assets, spend ledger, review feedback, Instagram metrics, key-value store."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import EpisodePlan, JobStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  episode_no    INTEGER NOT NULL,
  plan_json     TEXT    NOT NULL,
  status        TEXT    NOT NULL,
  caption       TEXT,
  video_path    TEXT,
  public_url    TEXT,
  ig_media_id   TEXT,
  tg_message_id INTEGER,
  publish_after TEXT,
  error         TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shots (
  job_id       INTEGER NOT NULL REFERENCES jobs(id),
  idx          INTEGER NOT NULL,
  keyframe_url TEXT,
  video_url    TEXT,
  local_path   TEXT,
  attempts     INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (job_id, idx)
);
CREATE TABLE IF NOT EXISTS spend (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id      INTEGER,
  provider    TEXT NOT NULL,
  description TEXT NOT NULL,
  amount_usd  REAL NOT NULL,
  created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS feedback (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id     INTEGER,
  kind       TEXT NOT NULL,              -- note | approved | rejected | retake
  text       TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS metrics (
  job_id     INTEGER PRIMARY KEY,
  media_id   TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  data_json  TEXT NOT NULL               -- latest lifetime insights, e.g. {"views": 2140, ...}
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- jobs ----
    def create_job(self, episode_no: int, plan: EpisodePlan) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO jobs (episode_no, plan_json, status, created_at, updated_at) VALUES (?,?,?,?,?)",
                (episode_no, plan.model_dump_json(), JobStatus.PLANNED, _now(), _now()),
            )
            job_id = int(cur.lastrowid)
            for i in range(len(plan.shots)):
                c.execute("INSERT INTO shots (job_id, idx) VALUES (?,?)", (job_id, i))
            return job_id

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def plan(self, job_id: int) -> EpisodePlan:
        job = self.get_job(job_id)
        if not job:
            raise KeyError(f"job {job_id} not found")
        return EpisodePlan.model_validate_json(job["plan_json"])

    def update_job(self, job_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))

    def jobs_with_status(self, *statuses: JobStatus) -> list[dict[str, Any]]:
        marks = ",".join("?" * len(statuses))
        with self._conn() as c:
            rows = c.execute(f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY id", statuses).fetchall()
            return [dict(r) for r in rows]

    def next_episode_no(self) -> int:
        """Rejected/failed episodes don't consume a number."""
        with self._conn() as c:
            row = c.execute(
                "SELECT COALESCE(MAX(episode_no), 0) FROM jobs WHERE status NOT IN (?, ?)",
                (JobStatus.REJECTED, JobStatus.FAILED),
            ).fetchone()
            return int(row[0]) + 1

    def past_titles(self, limit: int = 60) -> list[str]:
        with self._conn() as c:
            rows = c.execute(  # rejected/failed drafts don't use up a premise
                "SELECT plan_json FROM jobs WHERE status NOT IN (?, ?) ORDER BY id DESC LIMIT ?",
                (JobStatus.REJECTED, JobStatus.FAILED, limit),
            ).fetchall()
        out = []
        for r in rows:
            p = json.loads(r[0])
            out.append(f"{p.get('title')} ({p.get('category')})")
        return out

    # ---- shots ----
    def shots(self, job_id: int) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM shots WHERE job_id=? ORDER BY idx", (job_id,)).fetchall()
            return [dict(r) for r in rows]

    def update_shot(self, job_id: int, idx: int, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE shots SET {cols} WHERE job_id=? AND idx=?", (*fields.values(), job_id, idx))

    def bump_shot_attempts(self, job_id: int, idx: int) -> None:
        with self._conn() as c:
            c.execute("UPDATE shots SET attempts=attempts+1 WHERE job_id=? AND idx=?", (job_id, idx))

    # ---- spend ----
    def record_spend(self, provider: str, description: str, amount: float, job_id: int | None) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO spend (job_id, provider, description, amount_usd, created_at) VALUES (?,?,?,?,?)",
                (job_id, provider, description, amount, _now()),
            )

    def spend_since(self, iso_start: str) -> float:
        with self._conn() as c:
            row = c.execute(
                "SELECT COALESCE(SUM(amount_usd), 0) FROM spend WHERE created_at >= ?", (iso_start,)
            ).fetchone()
            return float(row[0])

    def spend_for_job(self, job_id: int) -> float:
        with self._conn() as c:
            row = c.execute(
                "SELECT COALESCE(SUM(amount_usd), 0) FROM spend WHERE job_id=?", (job_id,)
            ).fetchone()
            return float(row[0])

    # ---- feedback (your verdicts + notes) and Instagram metrics: what the series learns from ----
    def add_feedback(self, job_id: int | None, kind: str, text: str = "") -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO feedback (job_id, kind, text, created_at) VALUES (?,?,?,?)",
                (job_id, kind, text.strip(), _now()),
            )

    def feedback(self, limit: int = 40) -> list[dict[str, Any]]:
        """Newest first."""
        with self._conn() as c:
            rows = c.execute("SELECT * FROM feedback ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def save_metrics(self, job_id: int, media_id: str, data: dict[str, float]) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO metrics (job_id, media_id, fetched_at, data_json) VALUES (?,?,?,?) "
                "ON CONFLICT(job_id) DO UPDATE SET media_id=excluded.media_id, "
                "fetched_at=excluded.fetched_at, data_json=excluded.data_json",
                (job_id, media_id, _now(), json.dumps(data)),
            )

    def metrics(self) -> dict[int, dict[str, Any]]:
        """job id -> {"fetched_at": iso, "data": {...}}"""
        with self._conn() as c:
            rows = c.execute("SELECT job_id, fetched_at, data_json FROM metrics").fetchall()
        return {r[0]: {"fetched_at": r[1], "data": json.loads(r[2])} for r in rows}

    def last_evidence_at(self) -> str:
        """When the newest feedback or metric arrived ('' if none): lessons older than this are stale."""
        with self._conn() as c:
            row = c.execute(
                "SELECT MAX(t) FROM (SELECT MAX(created_at) AS t FROM feedback "
                "UNION ALL SELECT MAX(fetched_at) FROM metrics)"
            ).fetchone()
            return row[0] or ""

    # ---- kv ----
    def kv_get(self, key: str) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def kv_set(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
