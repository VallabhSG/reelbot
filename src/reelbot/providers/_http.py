from __future__ import annotations

import shutil
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential


def retryable(exc: BaseException) -> bool:
    """Retry network errors, 429 and 5xx; never retry other 4xx (bad key, bad schema...)."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return False


@retry(
    retry=retry_if_exception(retryable),
    stop=stop_after_attempt(4),
    wait=wait_exponential(2, max=30),
    reraise=True,
)
def download(url: str, dest: Path, http: httpx.Client | None = None) -> Path:
    """Download a provider output URL (or copy a file:// URL from the dry-run stubs) to dest."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    parsed = urlparse(url)
    if parsed.scheme == "file":
        shutil.copyfile(unquote(parsed.path), dest)
        return dest
    client = http or httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0), follow_redirects=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with client.stream("GET", url) as r:
        r.raise_for_status()
        with tmp.open("wb") as f:
            for chunk in r.iter_bytes(1 << 16):
                f.write(chunk)
    tmp.replace(dest)
    return dest
