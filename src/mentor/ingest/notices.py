"""Notice ingestion from the SAM.gov Get Opportunities v2 search.

For every configured NAICS code and the requested posting window: fetch pages through the
budgeted client, resolve agency paths into entities, upsert notices with their verbatim
``raw_json``, snapshot a version on first sight and on change, and queue attachments and
descriptions for the fetch pipeline. Serves docs/DESIGN.md §8 and §9 step 3.
"""

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from mentor import db, runs
from mentor.config import Settings
from mentor.sam.client import SamClient
from mentor.sam.models import Opportunity

SOURCE_ID = "sam_opportunities_api"

UPSERT_NOTICE = """
INSERT INTO notices (
    notice_id, solicitation_number, title, notice_type, full_parent_path_name,
    full_parent_path_code, agency_entity_id, naics_code, psc_code, set_aside_code,
    posted_at, response_deadline, place_of_performance, active, first_seen_at, last_seen_at,
    source_id, description_url, description_status, raw_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(notice_id) DO UPDATE SET
    solicitation_number = excluded.solicitation_number,
    title = excluded.title,
    notice_type = excluded.notice_type,
    full_parent_path_name = excluded.full_parent_path_name,
    full_parent_path_code = excluded.full_parent_path_code,
    agency_entity_id = excluded.agency_entity_id,
    naics_code = excluded.naics_code,
    psc_code = excluded.psc_code,
    set_aside_code = excluded.set_aside_code,
    posted_at = excluded.posted_at,
    response_deadline = excluded.response_deadline,
    place_of_performance = excluded.place_of_performance,
    active = excluded.active,
    last_seen_at = excluded.last_seen_at,
    description_url = excluded.description_url,
    source_id = excluded.source_id,
    raw_json = excluded.raw_json
"""


@dataclass(frozen=True)
class IngestResult:
    run_id: int
    notices_seen: int
    notices_new: int
    versions_added: int
    attachments_added: int
    requests_spent: int


def ingest_notices(
    conn: sqlite3.Connection, settings: Settings, *, posted_from: date, posted_to: date
) -> IngestResult:
    """Search every configured NAICS code for the window and upsert what comes back.

    Each page is one keyed request followed by one transaction, so a failure loses at most
    the page in flight; pages already committed stand. The run row is closed as failed with
    the error text on any exception, which is then re-raised.
    """
    if not settings.naics:
        raise ValueError("no NAICS codes configured (MENTOR_NAICS)")
    run_id = runs.start(conn, SOURCE_ID, posted_from=posted_from, posted_to=posted_to)
    now = db.utcnow()
    seen = new = versions = attachments = 0
    try:
        with SamClient(settings, conn, run_id) as client:
            for naics in settings.naics:
                for page in client.search_pages(posted_from, posted_to, naics):
                    # The keyed request and its api_requests row completed in autocommit
                    # mode before the page was yielded; only the page's writes go here.
                    conn.execute("BEGIN")
                    try:
                        for raw in page.opportunities_data:
                            is_new, versioned, added = _ingest_record(conn, raw, now)
                            seen += 1
                            new += is_new
                            versions += versioned
                            attachments += added
                        conn.execute("COMMIT")
                    except BaseException:
                        conn.execute("ROLLBACK")
                        raise
    except Exception as exc:
        runs.finish(conn, run_id, status="failed", records_returned=seen, error=str(exc))
        raise
    runs.finish(conn, run_id, status="succeeded", records_returned=seen)
    (spent,) = conn.execute(
        "SELECT requests_spent FROM ingestion_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    return IngestResult(run_id, seen, new, versions, attachments, spent)


def _ingest_record(
    conn: sqlite3.Connection, raw: dict[str, Any], now: str
) -> tuple[bool, bool, int]:
    """Upsert one search record. Returns (new, versioned, attachments_added)."""
    opp = Opportunity.model_validate(raw)
    raw_json = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    raw_hash = hashlib.sha256(raw_json.encode()).hexdigest()
    agency_id = resolve_agency_path(
        conn, opp.full_parent_path_code, opp.full_parent_path_name, SOURCE_ID, now
    )
    conn.execute(
        UPSERT_NOTICE,
        (
            opp.notice_id,
            opp.solicitation_number,
            opp.title,
            opp.notice_type,
            opp.full_parent_path_name,
            opp.full_parent_path_code,
            agency_id,
            opp.naics_code,
            opp.psc_code,
            # '', 'NONE', and null are three distinct upstream states; views may collapse them.
            opp.set_aside_code,
            opp.posted_at,
            _deadline_utc(opp.response_deadline),
            json.dumps(opp.place_of_performance) if opp.place_of_performance else None,
            int(opp.active),
            now,
            now,
            SOURCE_ID,
            opp.description_url,
            "pending" if opp.description_url else "none",
            raw_json,
        ),
    )
    last = conn.execute(
        "SELECT raw_hash FROM notice_versions WHERE notice_id = ? ORDER BY version_id DESC LIMIT 1",
        (opp.notice_id,),
    ).fetchone()
    new = last is None
    versioned = new or last[0] != raw_hash
    if versioned:
        conn.execute(
            "INSERT INTO notice_versions (notice_id, observed_at, raw_hash, raw_json)"
            " VALUES (?, ?, ?, ?)",
            (opp.notice_id, now, raw_hash, raw_json),
        )
    added = sum(
        conn.execute(
            "INSERT OR IGNORE INTO attachments (notice_id, url) VALUES (?, ?)",
            (opp.notice_id, url),
        ).rowcount
        for url in opp.resource_links
    )
    return new, versioned, added


def resolve_agency_path(
    conn: sqlite3.Connection, code: str | None, name: str | None, source_id: str, now: str
) -> int | None:
    """Find or create one entity per prefix of a dot-separated agency path; return the leaf.

    Names come from the matching segment of ``name`` when it has as many segments as
    ``code``; otherwise entities are named by their code prefix and the whole name string
    is recorded as an alias of the leaf (a name segment containing a dot is the usual cause).
    Existing entities only get ``last_seen_at`` bumped, so nothing is overwritten.
    """
    if not code:
        return None
    codes = code.split(".")
    names = name.split(".") if name else []
    if len(names) == len(codes):
        return resolve_agency_segments(conn, codes, names, source_id, now)
    leaf = resolve_agency_segments(conn, codes, [], source_id, now)
    if name and leaf is not None:
        record_alias(conn, leaf, name, source_id, now)
    return leaf


def resolve_agency_segments(
    conn: sqlite3.Connection, codes: list[str], names: list[str], source_id: str, now: str
) -> int | None:
    """One entity per prefix of ``codes``. ``names`` is parallel to ``codes`` or empty, in
    which case each entity is named by its code prefix. Returns the leaf entity id."""
    parent: int | None = None
    for depth, _ in enumerate(codes):
        prefix = ".".join(codes[: depth + 1])
        label = names[depth] if names else prefix
        (entity_id,) = conn.execute(
            "INSERT INTO entities (kind, name, agency_path_code, parent_entity_id, source_id,"
            " first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(agency_path_code) DO UPDATE SET last_seen_at = excluded.last_seen_at"
            " RETURNING entity_id",
            ("agency" if depth == 0 else "office", label, prefix, parent, source_id, now, now),
        ).fetchone()
        if names:
            record_alias(conn, entity_id, label, source_id, now)
        parent = entity_id
    return parent


def record_alias(
    conn: sqlite3.Connection, entity_id: int, alias: str, source_id: str, now: str
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO entity_aliases (alias, entity_id, source_id, method, confidence,"
        " resolved_at) VALUES (?, ?, ?, 'exact_key', 1.0, ?)",
        (alias, entity_id, source_id, now),
    )


def _deadline_utc(value: str | None) -> str | None:
    """'2026-09-14T08:00:00-04:00' -> '2026-09-14T12:00:00Z'. A missing offset is read as UTC."""
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime(db.TIMESTAMP_FORMAT)
