"""Budgeted fetch queue for notice descriptions, attachment manifests, and attachments.

Descriptions are keyed requests and spend the daily budget, so they go first, soonest
deadline first. Manifests and the files themselves are public, free of the budget, and read
afterwards in the same order with a politeness pause between every request. Every outcome is
written to the row; failures are recorded and not retried. Serves docs/DESIGN.md §4 and §8.

A notice's resourceLinks are a first observation and not the source of record: amendments add
files after a notice is ingested and the search window will not re-see it, so manifests are
re-read on an interval and whenever the notice has been amended since the last check. That is
why discovery lives here rather than in an adapter -- it is a recurring queue over the store,
not a stage of ingesting one file.

Each item is one autocommit UPDATE after its request, so there is no transaction to lose.
If the process dies between a description request and its UPDATE, the request is spent and
the row stays pending (one request to retry). A downloaded file whose UPDATE never ran is
rewritten idempotently on retry.
"""

import json
import re
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser

from orrery import db, query, runs
from orrery.config import Settings
from orrery.progress import Cancelled, Report, check, never, quiet
from orrery.quota import BudgetExceeded
from orrery.sam.client import (
    AttachmentTooLarge,
    ManifestShapeError,
    ManifestUnavailable,
    NoticeUnknown,
    SamClient,
    SamError,
)

SOURCE_ID = "sam_opportunities_api"

RECHECK_DAYS = 7
"""How long a checked manifest stands before it is read again. Amendments are usually
caught sooner than this: any change to a notice's own raw_json writes a notice_versions
row, and a row newer than the last check makes the manifest due immediately."""

# Notices matching any saved search's structured filters go first: the store is shared
# public data, so a notice any user wants is worth fetching before the rest.
_WANTED = f"""
WITH wanted AS (
    SELECT DISTINCT n.notice_id FROM notices AS n, saved_searches AS s
    WHERE n.active = 1 {query.notice_filter_sql("s.")}
)
"""

# LIMIT -1 is unbounded in SQLite.
PENDING_DESCRIPTIONS = (
    _WANTED
    + """
SELECT notice_id, description_url, response_deadline, title FROM notices
WHERE description_status = 'pending'
ORDER BY notice_id NOT IN (SELECT notice_id FROM wanted),
         response_deadline IS NULL, response_deadline, posted_at DESC, id
LIMIT :limit
"""
)

PENDING_ATTACHMENTS = (
    _WANTED
    + """
SELECT a.attachment_id, a.notice_id, a.url
FROM attachments AS a JOIN notices AS n USING (notice_id)
WHERE a.fetch_status = 'pending'
ORDER BY a.notice_id NOT IN (SELECT notice_id FROM wanted),
         n.response_deadline IS NULL, n.response_deadline, n.posted_at DESC, a.attachment_id
LIMIT :limit
"""
)


PENDING_MANIFESTS = (
    _WANTED
    + """
SELECT n.notice_id FROM notices AS n
WHERE n.manifest_status = 'pending'
   OR (n.manifest_status IN ('checked', 'failed') AND n.active = 1
       AND (n.response_deadline IS NULL OR n.response_deadline >= :now)
       AND (n.manifest_checked_at IS NULL
            OR n.manifest_checked_at < :due
            OR EXISTS (SELECT 1 FROM notice_versions AS v
                       WHERE v.notice_id = n.notice_id
                         AND v.observed_at > n.manifest_checked_at)))
ORDER BY n.notice_id NOT IN (SELECT notice_id FROM wanted),
         n.response_deadline IS NULL, n.response_deadline, n.posted_at DESC, n.id
LIMIT :limit
"""
)
"""Never checked, or checked and due again: still active, still open, and either the interval
has passed or the notice has been amended since. An 'unknown' notice is never asked about
again, and an inactive one is never re-checked."""

# One row per attachment the manifest names. Metadata is refreshed on every sighting; a row
# that has already been fetched or skipped keeps that status, and a pending row the manifest
# now says is unfetchable becomes skipped without a request ever being spent on it.
UPSERT_ATTACHMENT = """
INSERT INTO attachments (notice_id, url, filename, fetch_status, discovered_by, declared_size,
    mime_type, access_status, export_controlled, deleted_upstream, manifest_json, last_seen_at)
VALUES (:notice_id, :url, :filename, :status, 'manifest', :size, :mime, :access, :controlled,
    :deleted, :raw, :now)
ON CONFLICT (notice_id, url) DO UPDATE SET
    filename = coalesce(attachments.filename, excluded.filename),
    declared_size = excluded.declared_size,
    mime_type = excluded.mime_type,
    access_status = excluded.access_status,
    export_controlled = excluded.export_controlled,
    deleted_upstream = excluded.deleted_upstream,
    manifest_json = excluded.manifest_json,
    last_seen_at = excluded.last_seen_at,
    fetch_status = CASE
        WHEN attachments.fetch_status = 'pending' AND excluded.fetch_status = 'skipped'
        THEN 'skipped' ELSE attachments.fetch_status END
"""


def _pending(conn: sqlite3.Connection, sql: str, limit: int) -> list[tuple]:
    now = db.utcnow()
    due = (
        datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        - timedelta(days=RECHECK_DAYS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return conn.execute(sql, {"limit": limit, "now": now, "due": due}).fetchall()


@dataclass(frozen=True)
class FetchResult:
    run_id: int
    descriptions_fetched: int
    descriptions_failed: int
    manifests_checked: int
    manifests_failed: int
    attachments_found: int
    attachments_fetched: int
    attachments_failed: int
    attachments_skipped: int
    requests_spent: int
    budget_exhausted: bool
    failures: dict[str, int] = field(default_factory=dict)
    """How many failures of each kind this run recorded, across all three stages. A count
    alone says how much went wrong; the kinds say whether any of it is worth trying again."""


@dataclass(frozen=True)
class QueuedNotice:
    notice_id: str
    response_deadline: str | None
    title: str


@dataclass(frozen=True)
class QueueStatus:
    descriptions_pending: int
    attachments_pending: int
    next_descriptions: list[QueuedNotice]


def queue_status(conn: sqlite3.Connection, *, limit: int = 5) -> QueueStatus:
    """Pending counts plus the first ``limit`` descriptions in priority order. Reads only."""
    (descriptions,) = conn.execute(
        "SELECT count(*) FROM notices WHERE description_status = 'pending'"
    ).fetchone()
    (attachments,) = conn.execute(
        "SELECT count(*) FROM attachments WHERE fetch_status = 'pending'"
    ).fetchone()
    rows = _pending(conn, PENDING_DESCRIPTIONS, limit)
    return QueueStatus(
        descriptions_pending=descriptions,
        attachments_pending=attachments,
        next_descriptions=[QueuedNotice(nid, deadline, title) for nid, _, deadline, title in rows],
    )


def fetch_pending(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    budget: int | None = None,
    max_attachments: int | None = None,
    max_manifests: int | None = None,
    report: Report = quiet,
    cancelled: Cancelled = never,
) -> FetchResult:
    """Descriptions first (keyed, budgeted), then manifests, then downloads (both public and
    delayed).

    ``budget`` caps descriptions this run; the daily quota is enforced by the client, which
    raises BudgetExceeded before spending, so the effective cap is the smaller of the two and
    ``budget_exhausted`` reports only the quota case. ``max_manifests`` caps the notices asked
    about and ``max_attachments`` caps downloads; None means every due or pending row.

    The manifest stage runs before downloads so that files it discovers, and the sizes and
    types it declares, are available to the download stage in the same run.
    """
    run_id = runs.start(conn, SOURCE_ID)
    d_fetched = d_failed = m_checked = m_failed = found = 0
    a_fetched = a_failed = a_skipped = 0
    exhausted = False
    failures: Counter = Counter()
    shape_error: ManifestShapeError | None = None
    try:
        with SamClient(settings, conn, run_id) as client:
            if settings.sam_api_key is None:
                # The rest of this run needs no key. Say so rather than failing: a store built
                # from the bulk extract has its descriptions already.
                report("descriptions: skipped, ORRERY_SAM_API_KEY is not set")
            else:
                d_fetched, d_failed, exhausted = _fetch_descriptions(
                    conn, client, -1 if budget is None else budget, failures, report, cancelled
                )
            m_checked, m_failed, found, shape_error = _fetch_manifests(
                conn, client, settings, -1 if max_manifests is None else max_manifests,
                failures, report, cancelled,
            )  # fmt: skip
            a_fetched, a_failed, a_skipped = _fetch_attachments(
                conn, client, settings, run_id, -1 if max_attachments is None else max_attachments,
                failures, report, cancelled,
            )  # fmt: skip
    except Exception as exc:
        processed = d_fetched + d_failed + m_checked + a_fetched + a_failed + a_skipped
        runs.finish(conn, run_id, status="failed", records_returned=processed, error=str(exc))
        raise
    processed = d_fetched + d_failed + m_checked + a_fetched + a_failed + a_skipped
    if shape_error is not None:
        # The manifest endpoint has no published contract. Downloads already queued still ran;
        # the run closes failed naming the field that broke, so it is visible rather than a
        # slow drift into fetching nothing.
        runs.finish(
            conn, run_id, status="failed", records_returned=processed, error=str(shape_error)
        )
        raise shape_error
    runs.finish(conn, run_id, status="succeeded", records_returned=processed)
    (spent,) = conn.execute(
        "SELECT requests_spent FROM ingestion_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    return FetchResult(
        run_id, d_fetched, d_failed, m_checked, m_failed, found,
        a_fetched, a_failed, a_skipped, spent, exhausted, dict(sorted(failures.items())),
    )  # fmt: skip


def _fetch_descriptions(
    conn: sqlite3.Connection,
    client: SamClient,
    limit: int,
    failures: Counter,
    report: Report = quiet,
    cancelled: Cancelled = never,
) -> tuple[int, int, bool]:
    """Returns (fetched, failed, budget_exhausted); ``failures`` tallies the kinds."""
    fetched = failed = 0
    rows = _pending(conn, PENDING_DESCRIPTIONS, limit)
    for notice_id, url, _deadline, title in rows:
        check(cancelled)
        try:
            text = _html_to_text(client.get_description(url, notice_id=notice_id) or "")
        except BudgetExceeded:
            return fetched, failed, True
        except SamError as exc:
            _record_failure(
                conn, "UPDATE notices SET description_status = 'failed',"
                " description_failure_kind = ?, description_failure_detail = ?"
                " WHERE notice_id = ?",
                notice_id, exc, failures,
            )  # fmt: skip
            failed += 1
            continue
        conn.execute(
            "UPDATE notices SET description = ?, description_status = 'fetched',"
            " description_failure_kind = NULL, description_failure_detail = NULL"
            " WHERE notice_id = ?",
            (text, notice_id),
        )
        fetched += 1
        report(f"description: {title}")
    return fetched, failed, False


def _fail_manifest(
    conn: sqlite3.Connection, notice_id: str, status: str, exc: SamError, failures: Counter
) -> None:
    """Record a manifest read that did not produce a manifest. ``status`` stays the existing
    vocabulary -- ``unknown`` is terminal, ``failed`` comes round again -- and the kind says
    which of the several ways it failed this was."""
    conn.execute(
        "UPDATE notices SET manifest_status = ?, manifest_checked_at = ?,"
        " manifest_failure_kind = ?, manifest_failure_detail = ? WHERE notice_id = ?",
        (status, db.utcnow(), exc.kind, str(exc), notice_id),
    )
    failures[exc.kind] += 1


def _record_failure(
    conn: sqlite3.Connection, sql: str, row_id: object, exc: SamError, failures: Counter
) -> None:
    """Write one stage's failure with its reason and tally the kind. The detail is the
    exception's own message, which the client has already stripped of the API key."""
    conn.execute(sql, (exc.kind, str(exc), row_id))
    failures[exc.kind] += 1


def _human_bytes(count: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1024
    return f"{count:.1f} GB"


def _fetch_manifests(
    conn: sqlite3.Connection,
    client: SamClient,
    settings: Settings,
    limit: int,
    failures: Counter,
    report: Report = quiet,
    cancelled: Cancelled = never,
) -> tuple[int, int, int, ManifestShapeError | None]:
    """Read attachment manifests for notices that are due. Returns (checked, failed, found,
    shape error). No key, no quota. Pauses ``settings.fetch_delay`` between requests.

    Strict at the stage, never silent at the row: a notice SAM.gov does not know is recorded
    and never asked about again, a transient failure comes round under the interval rule, and
    a body this release cannot parse stops the stage on the first one and is handed back so
    the run closes failed rather than writing rows it does not understand.
    """
    checked = failed = found = offsite = 0
    cap = settings.max_attachment_bytes
    rows = _pending(conn, PENDING_MANIFESTS, limit)
    for index, (notice_id,) in enumerate(rows):
        check(cancelled)
        if index:
            time.sleep(settings.fetch_delay)
        try:
            items = client.get_attachment_manifest(notice_id)
        except NoticeUnknown as exc:
            _fail_manifest(conn, notice_id, "unknown", exc, failures)
            failed += 1
            continue
        except ManifestShapeError as exc:
            return checked, failed, found, exc
        except ManifestUnavailable as exc:
            _fail_manifest(conn, notice_id, "failed", exc, failures)
            failed += 1
            if exc.status_code == 429 or exc.status_code >= 500:
                report(f"manifests: stopping, SAM.gov answered HTTP {exc.status_code}")
                break
            continue
        except SamError as exc:
            _fail_manifest(conn, notice_id, "failed", exc, failures)
            failed += 1
            continue
        now = db.utcnow()
        (before,) = conn.execute(
            "SELECT count(*) FROM attachments WHERE notice_id = ?", (notice_id,)
        ).fetchone()
        for item in items:
            oversized = item.size is not None and item.size > cap
            fetchable = item.downloadable and not oversized
            offsite += item.kind == "link"
            conn.execute(
                UPSERT_ATTACHMENT,
                {
                    "notice_id": notice_id,
                    # A link entry lives on another portal; record where, not a download URL
                    # that would fetch that portal's HTML. A file entry's uri is SAM.gov's
                    # storage key rather than a location, so the download URL is built from
                    # the resource id instead.
                    "url": item.offsite_url or client.attachment_url(item.resource_id),
                    "filename": item.name,
                    "status": "pending" if fetchable else "skipped",
                    "size": item.size,
                    "mime": item.mime_type,
                    "access": item.access_status,
                    "controlled": int(item.export_controlled),
                    "deleted": int(item.deleted),
                    "raw": json.dumps(item.raw, sort_keys=True),
                    "now": now,
                },
            )
        (after,) = conn.execute(
            "SELECT count(*) FROM attachments WHERE notice_id = ?", (notice_id,)
        ).fetchone()
        found += after - before
        conn.execute(
            "UPDATE notices SET manifest_status = 'checked', manifest_checked_at = ?,"
            " manifest_failure_kind = NULL, manifest_failure_detail = NULL"
            " WHERE notice_id = ?",
            (now, notice_id),
        )
        checked += 1
        if items:
            report(f"manifest: {notice_id} lists {len(items)} attachments")
    if found:
        (pending_bytes,) = conn.execute(
            "SELECT coalesce(sum(declared_size), 0) FROM attachments WHERE fetch_status = 'pending'"
        ).fetchone()
        report(f"manifests: {found} attachments found, {_human_bytes(pending_bytes)} to download")
    if offsite:
        # #5: naming these is the difference between "we do not fetch these" and "12 failures".
        report(f"manifests: {offsite} live on portals orrery does not fetch")
    return checked, failed, found, None


def _fetch_attachments(
    conn: sqlite3.Connection,
    client: SamClient,
    settings: Settings,
    run_id: int,
    limit: int,
    failures: Counter,
    report: Report = quiet,
    cancelled: Cancelled = never,
) -> tuple[int, int, int]:
    """Returns (fetched, failed, skipped). Pauses ``settings.fetch_delay`` between downloads."""
    fetched = failed = skipped = 0
    rows = _pending(conn, PENDING_ATTACHMENTS, limit)
    for index, (attachment_id, notice_id, url) in enumerate(rows):
        check(cancelled)
        if index:
            time.sleep(settings.fetch_delay)
        try:
            result = client.download(url, settings.data_dir / "attachments" / notice_id)
        except SamError as exc:
            status = "skipped" if isinstance(exc, AttachmentTooLarge) else "failed"
            conn.execute(
                "UPDATE attachments SET fetch_status = ?, fetched_at = ?, ingestion_run_id = ?,"
                " failure_kind = ?, failure_detail = ? WHERE attachment_id = ?",
                (status, db.utcnow(), run_id, exc.kind, str(exc), attachment_id),
            )
            failures[exc.kind] += 1
            if status == "skipped":
                skipped += 1
            else:
                failed += 1
            continue
        conn.execute(
            "UPDATE attachments SET filename = ?, path = ?, content_hash = ?, fetched_at = ?,"
            " ingestion_run_id = ?, fetch_status = 'fetched', failure_kind = NULL,"
            " failure_detail = NULL WHERE attachment_id = ?",
            (
                result.filename,
                result.path.relative_to(settings.data_dir).as_posix(),
                result.sha256,
                db.utcnow(),
                run_id,
                attachment_id,
            ),
        )
        fetched += 1
        report(f"attachment: {result.filename}")
    return fetched, failed, skipped


_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "ul", "ol", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6", "pre"}
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()  # convert_charrefs=True decodes entities
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _html_to_text(html: str) -> str:
    """Drop tags, turn block elements into line breaks, decode entities, tidy whitespace."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(parser.parts))
    return re.sub(r"\s*\n\s*(?:\n\s*)*", "\n", text).strip()
