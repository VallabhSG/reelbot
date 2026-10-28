"""Public hosting for the final MP4. Instagram's API pulls video from a public URL, so the file goes to
an S3-compatible bucket (Cloudflare R2 has free egress, which suits this)."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Protocol

from .config import Settings


class Storage(Protocol):
    def put_public(self, path: Path, key_prefix: str) -> tuple[str, str]: ...
    def delete(self, key: str) -> None: ...


class S3Storage:
    def __init__(self, s: Settings):
        import boto3

        missing = [n for n in ("s3_endpoint_url", "s3_bucket", "s3_public_base_url") if not getattr(s, n)]
        if missing:
            raise ValueError(f"missing storage settings: {', '.join(m.upper() for m in missing)}")
        self.bucket = s.s3_bucket
        self.public_base = s.s3_public_base_url.rstrip("/")
        self.client = boto3.client(
            "s3",
            endpoint_url=s.s3_endpoint_url,
            aws_access_key_id=s.s3_access_key_id,
            aws_secret_access_key=s.s3_secret_access_key,
            region_name="auto",
        )

    def put_public(self, path: Path, key_prefix: str) -> tuple[str, str]:
        key = f"{key_prefix.strip('/')}/{uuid.uuid4().hex}{path.suffix}"
        self.client.upload_file(str(path), self.bucket, key, ExtraArgs={"ContentType": "video/mp4"})
        return key, f"{self.public_base}/{key}"

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)


class LocalStorage:
    """Dry-run: pretend to host by returning a file:// URL."""

    def put_public(self, path: Path, key_prefix: str) -> tuple[str, str]:
        return str(path), path.resolve().as_uri()

    def delete(self, key: str) -> None:
        pass
