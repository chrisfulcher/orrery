"""USAspending client: a filtered award download, prepared asynchronously by the service.

No key and no quota: USAspending is open. Politeness is the only constraint, so one download
is requested per run and its status is polled with a delay that starts at ``fetch_delay`` and
doubles to thirty seconds. Requests go only to ``MENTOR_USASPENDING_BASE_URL`` and, for the
finished file, to a host under the same domain (``files.usaspending.gov``); any other host in
a response is refused. Serves docs/DESIGN.md §5 source 4.
"""

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from mentor import __version__
from mentor.config import Settings

DOWNLOAD_PATH = "/api/v2/download/awards/"
MAX_POLL_DELAY = 30.0


class UsaspendingError(Exception):
    """A USAspending request failed or the download could not be prepared."""


@dataclass(frozen=True)
class DownloadTicket:
    """What the download request returns: the file's name, its URL, and where to poll."""

    file_name: str
    file_url: str
    status_url: str


class UsaspendingClient:
    def __init__(
        self,
        settings: Settings,
        http: httpx.Client | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base = settings.usaspending_base_url.rstrip("/")
        self._host = httpx.URL(self._base).host
        self._delay = settings.fetch_delay
        self._sleep = sleep
        self._owns_http = http is None
        self._http = http or httpx.Client(
            timeout=httpx.Timeout(30.0, read=300.0),
            headers={"User-Agent": f"mentor/{__version__}"},
        )

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> "UsaspendingClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def request_awards_download(self, filters: dict) -> DownloadTicket:
        """Ask for a CSV of award summaries matching ``filters``; returns immediately."""
        url = httpx.URL(self._base + DOWNLOAD_PATH)
        body = self._send("POST", url, json={"filters": filters, "file_format": "csv"}).json()
        try:
            return DownloadTicket(body["file_name"], body["file_url"], body["status_url"])
        except (KeyError, TypeError) as exc:
            raise UsaspendingError(f"{url}: download response missing {exc}") from exc

    def wait_until_ready(self, ticket: DownloadTicket, *, max_wait: float = 900.0) -> int:
        """Poll until the file is finished; returns the row count the service reports.

        A ``failed`` status raises with the service's message. Preparation took five minutes
        for a ten-thousand-row slice when probed, so ``max_wait`` is generous.
        """
        url = httpx.URL(ticket.status_url)
        delay = self._delay
        waited = 0.0
        while True:
            body = self._send("GET", url).json()
            status = body.get("status")
            if status == "finished":
                return int(body.get("total_rows") or 0)
            if status == "failed":
                raise UsaspendingError(
                    f"{ticket.file_name}: {body.get('message') or 'download failed'}"
                )
            if waited >= max_wait:
                raise UsaspendingError(f"{ticket.file_name}: not ready after {max_wait:.0f} s")
            self._sleep(delay)
            waited += delay
            delay = min(max(delay * 2, 1.0), MAX_POLL_DELAY)

    def download(self, ticket: DownloadTicket, dest_dir: Path) -> Path:
        """Stream the finished zip into ``dest_dir``; never leaves a partial file behind."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / Path(ticket.file_name).name
        tmp = dest.with_name(dest.name + ".part")
        url = httpx.URL(ticket.file_url)
        self._check_host(url)
        try:
            with (
                open(tmp, "wb") as out,
                self._http.stream("GET", url, follow_redirects=False) as response,
            ):
                if not response.is_success:
                    raise UsaspendingError(f"{url}: HTTP {response.status_code}")
                for chunk in response.iter_bytes():
                    out.write(chunk)
        except BaseException as exc:
            tmp.unlink(missing_ok=True)
            if isinstance(exc, httpx.HTTPError):
                raise UsaspendingError(f"{url}: {type(exc).__name__}: {exc}") from exc
            raise
        os.replace(tmp, dest)
        return dest

    def _send(self, method: str, url: httpx.URL, **kwargs: object) -> httpx.Response:
        self._check_host(url)
        try:
            response = self._http.request(method, url, follow_redirects=False, **kwargs)
        except httpx.HTTPError as exc:
            raise UsaspendingError(f"{url}: {type(exc).__name__}: {exc}") from exc
        if not response.is_success:
            raise UsaspendingError(f"{url}: HTTP {response.status_code}")
        return response

    def _check_host(self, url: httpx.URL) -> None:
        """The API host itself, or a sibling under its domain (the file host)."""
        domain = self._host.split(".", 1)[1] if self._host.count(".") >= 2 else self._host
        if url.host != self._host and not url.host.endswith("." + domain):
            raise UsaspendingError(f"refusing to contact {url.host}")
