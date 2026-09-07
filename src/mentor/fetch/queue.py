"""Budgeted fetch queue for notice descriptions and attachments.

Descriptions are keyed requests and spend the daily budget, so they go first, soonest
deadline first. Attachments are public files, free of the budget, fetched afterwards in the
same order with a politeness pause. Every outcome is written to the row; failures are
recorded and not retried. Serves docs/DESIGN.md §4 and §8.

Each item is one autocommit UPDATE after its request, so there is no transaction to lose.
If the process dies between a description request and its UPDATE, the request is spent and
the row stays pending (one request to retry). A downloaded file whose UPDATE never ran is
rewritten idempotently on retry.
"""

import re
import sqlite3
import time
from dataclasses import dataclass
from html.parser import HTMLParser

from mentor import db, query, runs
from mentor.config import Settings
from mentor.quota import BudgetExceeded
from mentor.sam.client import AttachmentTooLarge, SamClient, SamError

SOURCE_ID = "sam_opportunities_api"

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


def _pending(conn: sqlite3.Connection, sql: str, limit: int) -> list[tuple]:
    return conn.execute(sql, {"limit": limit, "now": db.utcnow()}).fetchall()


@dataclass(frozen=True)
class FetchResult:
    run_id: int
    descriptions_fetched: int
    descriptions_failed: int
    attachments_fetched: int
    attachments_failed: int
    attachments_skipped: int
    requests_spent: int
    budget_exhausted: bool


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
) -> FetchResult:
    """Descriptions first (keyed, budgeted), then attachments (public, delayed).

    ``budget`` caps descriptions this run; the daily quota is enforced by the client, which
    raises BudgetExceeded before spending, so the effective cap is the smaller of the two and
    ``budget_exhausted`` reports only the quota case. ``max_attachments`` caps downloads;
    None means every pending row.
    """
    run_id = runs.start(conn, SOURCE_ID)
    d_fetched = d_failed = a_fetched = a_failed = a_skipped = 0
    exhausted = False
    try:
        with SamClient(settings, conn, run_id) as client:
            d_fetched, d_failed, exhausted = _fetch_descriptions(
                conn, client, -1 if budget is None else budget
            )
            a_fetched, a_failed, a_skipped = _fetch_attachments(
                conn, client, settings, run_id, -1 if max_attachments is None else max_attachments
            )
    except Exception as exc:
        processed = d_fetched + d_failed + a_fetched + a_failed + a_skipped
        runs.finish(conn, run_id, status="failed", records_returned=processed, error=str(exc))
        raise
    processed = d_fetched + d_failed + a_fetched + a_failed + a_skipped
    runs.finish(conn, run_id, status="succeeded", records_returned=processed)
    (spent,) = conn.execute(
        "SELECT requests_spent FROM ingestion_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    return FetchResult(
        run_id, d_fetched, d_failed, a_fetched, a_failed, a_skipped, spent, exhausted
    )


def _fetch_descriptions(
    conn: sqlite3.Connection, client: SamClient, limit: int
) -> tuple[int, int, bool]:
    """Returns (fetched, failed, budget_exhausted)."""
    fetched = failed = 0
    rows = _pending(conn, PENDING_DESCRIPTIONS, limit)
    for notice_id, url, _deadline, _title in rows:
        try:
            text = _html_to_text(client.get_description(url, notice_id=notice_id) or "")
        except BudgetExceeded:
            return fetched, failed, True
        except SamError:
            conn.execute(
                "UPDATE notices SET description_status = 'failed' WHERE notice_id = ?",
                (notice_id,),
            )
            failed += 1
            continue
        conn.execute(
            "UPDATE notices SET description = ?, description_status = 'fetched'"
            " WHERE notice_id = ?",
            (text, notice_id),
        )
        fetched += 1
    return fetched, failed, False


def _fetch_attachments(
    conn: sqlite3.Connection, client: SamClient, settings: Settings, run_id: int, limit: int
) -> tuple[int, int, int]:
    """Returns (fetched, failed, skipped). Pauses ``settings.fetch_delay`` between downloads."""
    fetched = failed = skipped = 0
    rows = _pending(conn, PENDING_ATTACHMENTS, limit)
    for index, (attachment_id, notice_id, url) in enumerate(rows):
        if index:
            time.sleep(settings.fetch_delay)
        try:
            result = client.download(url, settings.data_dir / "attachments" / notice_id)
        except SamError as exc:
            status = "skipped" if isinstance(exc, AttachmentTooLarge) else "failed"
            conn.execute(
                "UPDATE attachments SET fetch_status = ?, fetched_at = ?, ingestion_run_id = ?"
                " WHERE attachment_id = ?",
                (status, db.utcnow(), run_id, attachment_id),
            )
            if status == "skipped":
                skipped += 1
            else:
                failed += 1
            continue
        conn.execute(
            "UPDATE attachments SET filename = ?, path = ?, content_hash = ?, fetched_at = ?,"
            " ingestion_run_id = ?, fetch_status = 'fetched' WHERE attachment_id = ?",
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
