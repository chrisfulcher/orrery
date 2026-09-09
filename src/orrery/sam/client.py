"""SAM.gov client with local quota accounting.

Every keyed request (search pages, notice descriptions, entity lookups, the entity extract)
goes through ``_keyed_request``: budget check, ``api_requests`` row, send, row update.
Attachment downloads are public, carry no key, and are not counted. The API key never
appears in a stored URL, an exception message, or a log line.
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

from orrery import __version__, quota
from orrery.config import Settings
from orrery.sam.models import SearchPage

# httpx logs every request URL at INFO; ours carry the key.
logging.getLogger("httpx").setLevel(logging.WARNING)

SEARCH_PATH = "/opportunities/v2/search"
ENTITIES_PATH = "/entity-information/v3/entities"
EXTRACTS_PATH = "/data-services/v1/extracts"
ENTITY_EXTRACT_PARAMS = {
    "fileType": "ENTITY", "sensitivity": "PUBLIC", "frequency": "MONTHLY", "charset": "UTF-8"
}  # fmt: skip
MANIFEST_PATH = "/api/prod/opps/v3/opportunities/{notice_id}/resources"
MANIFEST_ACCEPT = "application/hal+json"  # a plain Accept header is answered with a 406
FILE_PATH = "/api/prod/opps/v3/opportunities/resources/files/{resource_id}/download"
ENTITIES_PER_REQUEST = 10  # the v3 page size cap
MAX_WINDOW = timedelta(days=365)
DATE_FORMAT = "%m/%d/%Y"


class SamError(Exception):
    """A SAM.gov request failed. The message never contains the API key."""


class AttachmentTooLarge(SamError):
    """The file exceeds ORRERY_MAX_ATTACHMENT_BYTES; the queue records it as skipped."""


class NoticeUnknown(SamError):
    """SAM.gov has no notice with this id. Terminal for that notice, never retried."""


class ManifestUnavailable(SamError):
    """The manifest endpoint answered with a status we cannot use. ``status_code`` lets the
    queue tell a per-notice problem from one that should stop the whole stage."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class ManifestShapeError(SamError):
    """The manifest response was not the shape this release pins to. The endpoint has no
    published contract, so this is the expected way it breaks; the queue stops the manifest
    stage on the first one rather than writing rows it does not understand."""


@dataclass(frozen=True)
class ManifestItem:
    """One attachment as the manifest states it. ``raw`` is kept so a shape change is visible
    in the store rather than only in a traceback."""

    resource_id: str
    name: str
    kind: str
    """``file`` or ``link``. A link entry is a URL on another procurement portal (PIEE and
    FedConnect are common), not a document SAM.gov holds: it has no name, no type and size 0.
    Recording it is worth doing -- it says where the solicitation actually lives -- but it is
    not something to download."""
    uri: str | None
    mime_type: str | None
    size: int | None
    access_status: str | None
    export_controlled: bool
    deleted: bool
    raw: dict

    @property
    def public(self) -> bool:
        return self.access_status == "public" and not self.export_controlled

    @property
    def downloadable(self) -> bool:
        """Whether fetching this would produce a document. See #5: an off-site link fetched
        blind yields a portal's HTML, or a failure indistinguishable from a broken network."""
        return self.kind == "file" and self.public and not self.deleted


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    filename: str
    sha256: str
    size: int


class SamClient:
    """One client per ingestion run; every keyed request is attributed to ``run_id``.

    The client has a keyed half (search, descriptions, entities, the entity extract) and an
    unkeyed half (attachment downloads). The unkeyed half costs no quota and needs no key, so
    the key is optional here and required by ``_keyed_request`` instead: a store can be built
    from the free bulk extract and its attachments with no SAM.gov credentials at all.
    """

    def __init__(
        self,
        settings: Settings,
        conn: sqlite3.Connection,
        run_id: int,
        http: httpx.Client | None = None,
    ) -> None:
        self._key = settings.sam_api_key
        """None is allowed: the unkeyed half of this client (attachment downloads, and the
        attachment manifest) is free and must work without an API key. Every keyed method
        refuses in ``_keyed_request`` instead."""
        self._api_host = httpx.URL(settings.sam_base_url).host
        self._settings = settings
        self._conn = conn
        self._run_id = run_id
        self._http = http or httpx.Client(
            timeout=30.0, headers={"User-Agent": f"orrery/{__version__}"}
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

    def get_entities(self, ueis: list[str]) -> dict:
        """Public registration records for up to ten UEIs in one keyed request. Points of
        contact are not requested."""
        if not 0 < len(ueis) <= ENTITIES_PER_REQUEST:
            raise ValueError(f"1 to {ENTITIES_PER_REQUEST} UEIs per request, got {len(ueis)}")
        return self._keyed_request(
            self._settings.sam_base_url + ENTITIES_PATH,
            {
                "ueiSAM": ",".join(ueis),
                "includeSections": "entityRegistration,coreData,assertions",
                "size": ENTITIES_PER_REQUEST,
            },
        ).json()

    def download_entity_extract(self, dest_dir: Path) -> Path:
        """The public monthly entity extract: one keyed request, answered with a redirect to a
        presigned file URL that carries no key and is streamed without one. The file keeps
        the name the service gives it. Never leaves a partial file behind."""
        response = self._keyed_request(
            self._settings.sam_base_url + EXTRACTS_PATH, ENTITY_EXTRACT_PARAMS, redirect_ok=True
        )
        dest_dir.mkdir(parents=True, exist_ok=True)
        if not response.is_redirect:
            path = dest_dir / "SAM_PUBLIC_UTF-8_MONTHLY_V2.ZIP"
            tmp = path.with_name(path.name + ".part")
            tmp.write_bytes(response.content)
            os.replace(tmp, path)
            return path
        location = httpx.URL(response.headers["location"])
        if "api_key" in location.params or self._key.get_secret_value() in str(location):
            raise SamError("refusing to follow a redirect that carries the API key")
        path = dest_dir / (Path(unquote_plus(location.path)).name or "SAM_PUBLIC_MONTHLY.ZIP")
        tmp = path.with_name(path.name + ".part")
        try:
            with (
                open(tmp, "wb") as out,
                self._http.stream("GET", location, follow_redirects=False) as stream,
            ):
                if not stream.is_success:
                    raise SamError(f"{location.host}: HTTP {stream.status_code}")
                for chunk in stream.iter_bytes():
                    out.write(chunk)
        except BaseException as exc:
            tmp.unlink(missing_ok=True)
            if isinstance(exc, httpx.HTTPError):
                raise SamError(f"{location.host}: {type(exc).__name__}: {exc}") from exc
            raise
        os.replace(tmp, path)
        return path

    def download(self, url: str, dest_dir: Path) -> DownloadResult:
        """Fetch a public attachment. No key is sent and no ``api_requests`` row is written.

        Raises ``AttachmentTooLarge`` (a ``SamError``) when the file exceeds the configured
        cap, checked against ``Content-Length`` before reading and against the bytes
        actually streamed as a backstop. Never leaves a partial file behind.
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        limit = self._settings.max_attachment_bytes
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
                declared = response.headers.get("content-length")
                if declared and int(declared) > limit:
                    raise AttachmentTooLarge(f"{url}: {declared} bytes exceeds {limit}")
                filename = _filename_from(response.headers.get("content-disposition"))
                for chunk in response.iter_bytes():
                    out.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                    if size > limit:
                        raise AttachmentTooLarge(f"{url}: exceeds {limit} bytes")
        except BaseException as exc:
            Path(tmp).unlink(missing_ok=True)
            if isinstance(exc, httpx.HTTPError):
                raise SamError(f"{url}: {type(exc).__name__}: {exc}") from exc
            raise
        sha256 = digest.hexdigest()
        path = dest_dir / f"{sha256[:16]}-{filename}"
        os.replace(tmp, path)
        return DownloadResult(path=path, filename=filename, sha256=sha256, size=size)

    def attachment_url(self, resource_id: str) -> str:
        """The public download URL for a manifest item; the same shape the API's
        ``resourceLinks`` carry, so both discovery paths produce identical rows."""
        return self._settings.sam_web_base_url + FILE_PATH.format(resource_id=resource_id)

    def get_attachment_manifest(self, notice_id: str) -> list[ManifestItem]:
        """Every attachment SAM.gov lists for a notice. No key, no quota, no api_requests row.

        An empty list means the manifest was read and named nothing, which is a result and not
        a failure -- the response omits ``_embedded`` entirely rather than carrying an empty
        one. Raises ``NoticeUnknown`` when SAM.gov does not recognise the id (answered with a
        400, not a 404), ``ManifestShapeError`` when the body is not the pinned shape, and
        ``SamError`` for anything else. See docs/notes/sam-manifest-probe.md.
        """
        url = self._settings.sam_web_base_url + MANIFEST_PATH.format(notice_id=notice_id)
        try:
            response = self._http.get(url, headers={"Accept": MANIFEST_ACCEPT})
        except httpx.HTTPError as exc:
            raise SamError(f"{url}: {type(exc).__name__}: {exc}") from exc
        if response.status_code == 400:
            raise NoticeUnknown(f"{url}: SAM.gov has no notice {notice_id}")
        if not response.is_success:
            raise ManifestUnavailable(f"{url}: HTTP {response.status_code}", response.status_code)
        try:
            body = response.json()
        except ValueError as exc:
            raise ManifestShapeError(f"{url}: response is not JSON") from exc
        if not isinstance(body, dict):
            raise ManifestShapeError(f"{url}: response is not an object")
        groups = (body.get("_embedded") or {}).get("opportunityAttachmentList") or []
        items = []
        for group in groups:
            for raw in (group or {}).get("attachments") or []:
                items.append(_manifest_item(raw, url))
        return items

    def _keyed_request(
        self,
        url: str,
        params: dict[str, str | int] | None = None,
        *,
        notice_id: str | None = None,
        redirect_ok: bool = False,
    ) -> httpx.Response:
        if self._key is None:
            raise SamError("ORRERY_SAM_API_KEY is not set")
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
        if not response.is_success and not (redirect_ok and response.is_redirect):
            raise SamError(f"{endpoint}: HTTP {response.status_code}")
        return response

    def _redact(self, text: str) -> str:
        if self._key is None:
            return text
        return text.replace(self._key.get_secret_value(), "[api_key]")


def _manifest_item(raw: object, url: str) -> ManifestItem:
    """One manifest entry, strictly. The resource id is the only field the row cannot be built
    without, so its absence is a shape change rather than a missing optional."""
    if not isinstance(raw, dict):
        raise ManifestShapeError(f"{url}: attachment entry is not an object")
    resource_id = raw.get("resourceId")
    if not isinstance(resource_id, str) or not resource_id:
        raise ManifestShapeError(f"{url}: attachment entry has no resourceId")
    size = raw.get("size")
    name = raw.get("name")
    kind = raw.get("type") or "file"
    uri = raw.get("uri")
    description = raw.get("description")
    if not (isinstance(name, str) and name) and kind == "link":
        # A link entry carries no name; its description is what a person would read.
        name = description if isinstance(description, str) and description else None
    return ManifestItem(
        resource_id=resource_id,
        name=name if isinstance(name, str) and name else "attachment",
        kind=kind if isinstance(kind, str) else "file",
        uri=uri if isinstance(uri, str) and uri else None,
        mime_type=raw.get("mimeType") or None,
        size=size if isinstance(size, int) else None,
        access_status=raw.get("accessStatus") or None,
        export_controlled=str(raw.get("exportControlled") or "0") not in ("0", "false", ""),
        deleted=str(raw.get("deletedFlag") or "0") not in ("0", "false", ""),
        raw=raw,
    )


def _filename_from(content_disposition: str | None) -> str:
    """Filename from a Content-Disposition header, form-decoded, with no path component."""
    message = Message()
    message["Content-Disposition"] = content_disposition or ""
    raw = message.get_filename()
    name = Path(unquote_plus(raw)).name.lstrip(".") if raw else ""
    return name or "attachment"
