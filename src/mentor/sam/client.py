"""SAM.gov client with local quota accounting.

Every keyed request (search pages, notice descriptions) goes through ``_keyed_request``:
budget check, ``api_requests`` row, send, row update. Attachment downloads are public,
carry no key, and are not counted. The API key never appears in a stored URL, an
exception message, or a log line.
"""

import hashlib
import logging
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from email.message import Message
from pathlib import Path
from urllib.parse import unquote_plus

import httpx

from mentor import __version__, quota
from mentor.config import Settings
from mentor.sam.models import SearchPage

# httpx logs every request URL at INFO; ours carry the key.
logging.getLogger("httpx").setLevel(logging.WARNING)

SEARCH_PATH = "/opportunities/v2/search"
MAX_WINDOW = timedelta(days=365)
DATE_FORMAT = "%m/%d/%Y"


class SamError(Exception):
    """A SAM.gov request failed. The message never contains the API key."""


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    filename: str
    sha256: str
    size: int


class SamClient:
    """One client per ingestion run; every keyed request is attributed to ``run_id``."""

    def __init__(
        self,
        settings: Settings,
        conn: sqlite3.Connection,
        run_id: int,
        http: httpx.Client | None = None,
    ) -> None:
        if settings.sam_api_key is None:
            raise SamError("MENTOR_SAM_API_KEY is not set")
        self._key = settings.sam_api_key
        self._api_host = httpx.URL(settings.sam_base_url).host
        self._settings = settings
        self._conn = conn
        self._run_id = run_id
        self._http = http or httpx.Client(
            timeout=30.0, headers={"User-Agent": f"mentor/{__version__}"}
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "SamClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def search_pages(
        self, posted_from: date, posted_to: date, naics: str, *, limit: int = 1000
    ) -> Iterator[SearchPage]:
        """Yield search pages for one NAICS code. Each page is one keyed request.

        ``BudgetExceeded`` can surface between pages; pages already yielded stand.
        """
        if posted_from > posted_to:
            raise ValueError("posted_from is after posted_to")
        if posted_to - posted_from > MAX_WINDOW:
            raise ValueError("SAM.gov limits a search window to one year")
        params: dict[str, str | int] = {
            "postedFrom": posted_from.strftime(DATE_FORMAT),
            "postedTo": posted_to.strftime(DATE_FORMAT),
            "ncode": naics,
            "limit": limit,
        }
        offset = 0
        while True:
            response = self._keyed_request(
                self._settings.sam_base_url + SEARCH_PATH, {**params, "offset": offset}
            )
            page = SearchPage.model_validate(response.json())
            yield page
            offset += len(page.opportunities_data)
            if not page.opportunities_data or offset >= page.total_records:
                return

    def get_description(self, url: str, *, notice_id: str | None = None) -> str:
        """Fetch a notice description (the v2 search response carries only its URL)."""
        return self._keyed_request(url, notice_id=notice_id).json()["description"]

    def download(self, url: str, dest_dir: Path) -> DownloadResult:
        """Fetch a public attachment. No key is sent and no ``api_requests`` row is written."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        fd, tmp = tempfile.mkstemp(dir=dest_dir, suffix=".part")
        try:
            with (
                os.fdopen(fd, "wb") as out,
                self._http.stream("GET", url, follow_redirects=True) as response,
            ):
                if not response.is_success:
                    raise SamError(f"{url}: HTTP {response.status_code}")
                filename = _filename_from(response.headers.get("content-disposition"))
                for chunk in response.iter_bytes():
                    out.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        sha256 = digest.hexdigest()
        path = dest_dir / f"{sha256[:16]}-{filename}"
        os.replace(tmp, path)
        return DownloadResult(path=path, filename=filename, sha256=sha256, size=size)

    def _keyed_request(
        self,
        url: str,
        params: dict[str, str | int] | None = None,
        *,
        notice_id: str | None = None,
    ) -> httpx.Response:
        target = httpx.URL(url).copy_merge_params(params or {})
        endpoint = str(target)
        if target.host != self._api_host:
            raise SamError(f"refusing to send the API key to {target.host}")
        if quota.remaining(self._conn, self._settings) <= 0:
            raise quota.BudgetExceeded(
                f"daily budget of {self._settings.sam_daily_budget} keyed requests is spent"
            )
        request_id = self._conn.execute(
            "INSERT INTO api_requests (run_id, endpoint, notice_id) VALUES (?, ?, ?)",
            (self._run_id, endpoint, notice_id),
        ).lastrowid
        keyed = target.copy_add_param("api_key", self._key.get_secret_value())
        try:
            response = self._http.get(keyed, follow_redirects=False)
        except httpx.HTTPError as exc:
            message = self._redact(f"{type(exc).__name__}: {exc}")
            self._conn.execute(
                "UPDATE api_requests SET error = ? WHERE request_id = ?", (message, request_id)
            )
            raise SamError(f"{endpoint}: {message}") from exc
        self._conn.execute(
            "UPDATE api_requests SET status_code = ?, response_bytes = ? WHERE request_id = ?",
            (response.status_code, len(response.content), request_id),
        )
        if not response.is_success:
            raise SamError(f"{endpoint}: HTTP {response.status_code}")
        return response

    def _redact(self, text: str) -> str:
        return text.replace(self._key.get_secret_value(), "[api_key]")


def _filename_from(content_disposition: str | None) -> str:
    """Filename from a Content-Disposition header, form-decoded, with no path component."""
    message = Message()
    message["Content-Disposition"] = content_disposition or ""
    raw = message.get_filename()
    name = Path(unquote_plus(raw)).name.lstrip(".") if raw else ""
    return name or "attachment"
