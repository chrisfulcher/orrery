"""One public file on disk, and the cached streamed download that puts it there.

Two of SAM.gov's file services need no key and no quota: the contract-opportunity extracts
(``ingest/bulk.py``) and the daily exclusions extract (``ingest/exclusions.py``). Both want
the same handling -- stream to a ``.part`` file and rename only on success, so a failed
download never leaves a truncated file where a whole one belongs, and record what the server
said in a ``.meta.json`` sidecar beside it, so a cached copy still knows when it was cut --
and differ only in the URL, the name on disk, and the error each raises. That is what lives
here. Serves docs/DESIGN.md §5 sources 2 and 3.
"""

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from orrery import __version__, db
from orrery.progress import Report, quiet


@dataclass(frozen=True)
class Extract:
    """One extract file on disk and what the server said about it.

    ``generated_at`` is the file's own cut, parsed from ``Last-Modified`` into the store's
    timestamp format, and is None whenever the server did not say: a file the user supplied,
    one downloaded before this metadata was kept, or a response with no such header.
    """

    path: Path
    generated_at: str | None = None
    etag: str | None = None


def meta_path(dest: Path) -> Path:
    return dest.with_name(dest.name + ".meta.json")


def write_meta(dest: Path, url: str, headers: httpx.Headers) -> None:
    """Record what the server said, verbatim, beside the file it said it about."""
    meta = {
        "url": url,
        "last_modified": headers.get("last-modified"),
        "etag": headers.get("etag"),
        "downloaded_at": db.utcnow(),
    }
    meta_path(dest).write_text(json.dumps(meta, indent=2), encoding="utf-8")


def read_meta(dest: Path) -> Extract:
    """Read the sidecar beside ``dest``. A missing or unreadable one is not an error: the
    extract is still usable, the caller simply learns nothing about when it was cut."""
    try:
        meta = json.loads(meta_path(dest).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Extract(dest)
    if not isinstance(meta, dict):
        return Extract(dest)
    return Extract(dest, http_date(meta.get("last_modified")), meta.get("etag"))


def http_date(value: object) -> str | None:
    """An RFC 7231 date as the store writes timestamps, or None if it is not one."""
    if not isinstance(value, str):
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def download(
    url: str,
    dest: Path,
    *,
    http: httpx.Client | None = None,
    report: Report = quiet,
    error: type[Exception] = RuntimeError,
) -> Extract:
    """Stream ``url`` into ``dest``, then write its sidecar and return the ``Extract``.

    The caller decides whether a copy on disk is fresh enough to skip this; by the time we
    are here the file is being fetched. ``error`` is the adapter's own exception class, so
    the job registry maps a failed download to that adapter's message.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    client = http or httpx.Client(
        timeout=httpx.Timeout(30.0, read=120.0), headers={"User-Agent": f"orrery/{__version__}"}
    )
    try:
        with (
            open(tmp, "wb") as out,
            client.stream("GET", url, follow_redirects=True) as response,
        ):
            if not response.is_success:
                raise error(f"{url}: HTTP {response.status_code}")
            headers = response.headers
            report(f"downloading {dest.name}")
            received = 0
            for chunk in response.iter_bytes():
                out.write(chunk)
                received += len(chunk)
                if received % (1 << 24) < len(chunk):
                    report(f"{received >> 20} MB")
            if received == 0:
                # SAM.gov answers 204 No Content for a dated file it has not published yet,
                # and 204 is a success. An empty body is never an extract, so it is the
                # caller's failure to handle rather than a zero-byte file left on disk.
                raise error(f"{url}: HTTP {response.status_code} with an empty body")
    except BaseException as exc:
        tmp.unlink(missing_ok=True)
        if isinstance(exc, httpx.HTTPError):
            raise error(f"{url}: {type(exc).__name__}: {exc}") from exc
        raise
    finally:
        if http is None:
            client.close()
    os.replace(tmp, dest)
    write_meta(dest, url, headers)
    return read_meta(dest)


def cached_today(dest: Path) -> Extract | None:
    """The file already downloaded today (UTC), or None. Delete it to force a fresh copy."""
    if dest.exists():
        modified = datetime.fromtimestamp(dest.stat().st_mtime, UTC).date()
        if modified == datetime.now(UTC).date():
            return read_meta(dest)
    return None
