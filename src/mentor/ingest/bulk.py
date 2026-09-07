"""Backfill from the SAM.gov Data Services contract-opportunity extracts.

The daily active extract and the per-fiscal-year archives are public CSV files: no key, no
quota. Rows in the configured NAICS slice become notices with their plain-text description
already in place. The API row wins on overlap: bulk only confirms it (``last_seen_at``,
``active``) and fills a description the API has not fetched. Progress is committed in
batches with a resume cursor on the run, keyed by the file's content and the NAICS filter,
so an interrupted run is a plain rerun. Serves docs/DESIGN.md §5 source 2 and §8.
"""

import csv
import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

import httpx

from mentor import __version__, db, runs
from mentor.config import Settings
from mentor.ingest.notices import _deadline_utc, resolve_agency_segments

SOURCE_ID = "sam_bulk_csv"
BATCH = 1000  # matched rows per transaction

EXTRACT_URL = (
    "https://sam.gov/api/prod/fileextractservices/v1/api/download/"
    "Contract%20Opportunities/{name}?privacy=Public"
)
ACTIVE_NAME = "datagov/ContractOpportunitiesFullCSV.csv"

COLUMNS = [
    "NoticeId", "Title", "Sol#", "Department/Ind.Agency", "CGAC", "Sub-Tier", "FPDS Code",
    "Office", "AAC Code", "PostedDate", "Type", "BaseType", "ArchiveType", "ArchiveDate",
    "SetASideCode", "SetASide", "ResponseDeadLine", "NaicsCode", "ClassificationCode",
    "PopStreetAddress", "PopCity", "PopState", "PopZip", "PopCountry", "Active",
    "AwardNumber", "AwardDate", "Award$", "Awardee", "PrimaryContactTitle",
    "PrimaryContactFullname", "PrimaryContactEmail", "PrimaryContactPhone",
    "PrimaryContactFax", "SecondaryContactTitle", "SecondaryContactFullname",
    "SecondaryContactEmail", "SecondaryContactPhone", "SecondaryContactFax",
    "OrganizationType", "State", "City", "ZipCode", "CountryCode", "AdditionalInfoLink",
    "Link", "Description",
]  # fmt: skip
REQUIRED_COLUMNS = frozenset(
    {
        "NoticeId", "Title", "Sol#", "Department/Ind.Agency", "CGAC", "Sub-Tier", "FPDS Code",
        "Office", "AAC Code", "PostedDate", "Type", "SetASideCode", "ResponseDeadLine",
        "NaicsCode", "ClassificationCode", "PopStreetAddress", "PopCity", "PopState", "PopZip",
        "PopCountry", "Active", "Description",
    }
)  # fmt: skip

UPSERT_BULK_NOTICE = """
INSERT INTO notices (
    notice_id, solicitation_number, title, notice_type, full_parent_path_name,
    full_parent_path_code, agency_entity_id, naics_code, psc_code, set_aside_code,
    posted_at, response_deadline, place_of_performance, active, first_seen_at, last_seen_at,
    source_id, description, description_status, raw_json
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
    description = excluded.description,
    description_status = excluded.description_status,
    source_id = excluded.source_id,
    raw_json = excluded.raw_json
"""

DEACTIVATE = """
UPDATE notices SET active = 0
WHERE active = 1 AND naics_code IN ({placeholders})
  AND last_seen_at < (SELECT min(started_at) FROM ingestion_runs WHERE cursor LIKE ? || ':%')
"""


class BulkError(Exception):
    """The extract could not be downloaded or is not the file we expect."""


@dataclass(frozen=True)
class BulkResult:
    run_id: int
    rows_read: int
    rows_matched: int
    notices_new: int
    notices_updated: int
    descriptions_filled: int
    versions_added: int
    notices_deactivated: int
    resumed_from: int


def archive_name(fiscal_year: int) -> str:
    return f"Archived%20Data/FY{fiscal_year}_archived_opportunities.csv"


def fetch_extract(
    dest_dir: Path, *, fiscal_year: int | None = None, http: httpx.Client | None = None
) -> Path:
    """Download the active extract (or one fiscal year's archive) into ``dest_dir`` unless a
    copy downloaded today (UTC) is already there. Streams to a .part file, then renames.
    Delete the file to force a fresh download."""
    name = ACTIVE_NAME if fiscal_year is None else archive_name(fiscal_year)
    dest = dest_dir / unquote(name).rsplit("/", 1)[-1]
    if dest.exists():
        modified = datetime.fromtimestamp(dest.stat().st_mtime, UTC).date()
        if modified == datetime.now(UTC).date():
            return dest
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    client = http or httpx.Client(
        timeout=httpx.Timeout(30.0, read=120.0), headers={"User-Agent": f"mentor/{__version__}"}
    )
    url = EXTRACT_URL.format(name=name)
    try:
        with (
            open(tmp, "wb") as out,
            client.stream("GET", url, follow_redirects=True) as response,
        ):
            if not response.is_success:
                raise BulkError(f"{url}: HTTP {response.status_code}")
            for chunk in response.iter_bytes():
                out.write(chunk)
    except BaseException as exc:
        tmp.unlink(missing_ok=True)
        if isinstance(exc, httpx.HTTPError):
            raise BulkError(f"{url}: {type(exc).__name__}: {exc}") from exc
        raise
    finally:
        if http is None:
            client.close()
    os.replace(tmp, dest)
    return dest


def ingest_bulk(
    conn: sqlite3.Connection,
    settings: Settings,
    path: Path,
    *,
    mark_inactive: bool = False,
    limit: int | None = None,
) -> BulkResult:
    """Stream one extract, writing rows in the NAICS slice in batches of ``BATCH``.

    ``limit`` caps matched rows this run. ``mark_inactive`` runs the deactivation pass after a
    complete pass; the CLI sets it only for the active extract. The run is closed as failed
    with the error on any exception; committed batches and their cursor stand.
    """
    if not settings.naics:
        raise ValueError("no NAICS codes configured (MENTOR_NAICS)")
    naics = set(settings.naics)
    key = _file_key(path, settings.naics)
    resumed_from = _resume_offset(conn, key)
    run_id = runs.start(conn, SOURCE_ID)
    now = db.utcnow()
    read = matched = new = updated = filled = versions = deactivated = 0
    in_batch = 0
    position = resumed_from
    complete = True

    def commit_batch() -> None:
        conn.execute(
            "UPDATE ingestion_runs SET cursor = ? WHERE run_id = ?", (f"{key}:{position}", run_id)
        )
        if conn.in_transaction:
            conn.execute("COMMIT")

    try:
        csv.field_size_limit(1 << 24)
        with open(path, encoding="cp1252", errors="replace", newline="") as handle:
            reader = csv.DictReader(handle, restkey="_extra")
            missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
            if missing:
                raise BulkError(f"{path.name}: missing columns {sorted(missing)}")
            for position, row in enumerate(reader, 1):
                if position <= resumed_from:
                    continue  # rows must be parsed to skip: quoted fields span lines
                read += 1
                if _opt(row["NaicsCode"]) not in naics:
                    continue
                if in_batch == 0:
                    conn.execute("BEGIN")
                is_new, was_filled, versioned = _ingest_row(conn, row, now)
                matched += 1
                in_batch += 1
                new += is_new
                updated += not is_new
                filled += was_filled
                versions += versioned
                if in_batch >= BATCH:
                    commit_batch()
                    in_batch = 0
                if limit is not None and matched >= limit:
                    complete = False
                    break
        commit_batch()
        if complete and mark_inactive:
            conn.execute("BEGIN")
            placeholders = ", ".join("?" * len(settings.naics))
            deactivated = conn.execute(
                DEACTIVATE.format(placeholders=placeholders), (*settings.naics, key)
            ).rowcount
            conn.execute("COMMIT")
    except Exception as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        runs.finish(conn, run_id, status="failed", records_returned=matched, error=str(exc))
        raise
    runs.finish(conn, run_id, status="succeeded", records_returned=matched)
    return BulkResult(
        run_id, read, matched, new, updated, filled, versions, deactivated, resumed_from
    )


def _ingest_row(conn: sqlite3.Connection, row: dict, now: str) -> tuple[bool, bool, bool]:
    """Write one extract row. Returns (new, description_filled, versioned)."""
    notice_id = row["NoticeId"].strip()
    description = _opt(row["Description"])
    active = int(row["Active"].strip() == "Yes")
    existing = conn.execute(
        "SELECT source_id, description_status FROM notices WHERE notice_id = ?", (notice_id,)
    ).fetchone()
    if existing is not None and existing[0] != SOURCE_ID:
        # The API row wins: confirm it, and fill a description it has not fetched.
        fill = description is not None and existing[1] != "fetched"
        if fill:
            conn.execute(
                "UPDATE notices SET last_seen_at = ?, active = ?, description = ?,"
                " description_status = 'fetched' WHERE notice_id = ?",
                (now, active, description, notice_id),
            )
        else:
            conn.execute(
                "UPDATE notices SET last_seen_at = ?, active = ? WHERE notice_id = ?",
                (now, active, notice_id),
            )
        return False, fill, False

    raw_json = json.dumps(row, sort_keys=True, separators=(",", ":"))
    raw_hash = hashlib.sha256(raw_json.encode()).hexdigest()
    codes, names = _agency_segments(row)
    agency_id = resolve_agency_segments(conn, codes, names, SOURCE_ID, now)
    place = {
        key: value
        for key, column in (
            ("street", "PopStreetAddress"),
            ("city", "PopCity"),
            ("state", "PopState"),
            ("zip", "PopZip"),
            ("country", "PopCountry"),
        )
        if (value := _opt(row[column]))
    }
    conn.execute(
        UPSERT_BULK_NOTICE,
        (
            notice_id,
            _opt(row["Sol#"]),
            row["Title"].strip(),
            _opt(row["Type"]),
            ".".join(names) or None,
            ".".join(codes) or None,
            agency_id,
            _opt(row["NaicsCode"]),
            _opt(row["ClassificationCode"]),
            _opt(row["SetASideCode"]),
            _opt(row["PostedDate"]),
            _deadline_utc(_opt(row["ResponseDeadLine"])),
            json.dumps(place) if place else None,
            active,
            now,
            now,
            SOURCE_ID,
            description,
            "fetched" if description else "none",
            raw_json,
        ),
    )
    last = conn.execute(
        "SELECT raw_hash FROM notice_versions WHERE notice_id = ? ORDER BY version_id DESC LIMIT 1",
        (notice_id,),
    ).fetchone()
    new = existing is None
    versioned = last is None or last[0] != raw_hash
    if versioned:
        conn.execute(
            "INSERT INTO notice_versions (notice_id, observed_at, raw_hash, raw_json)"
            " VALUES (?, ?, ?, ?)",
            (notice_id, now, raw_hash, raw_json),
        )
    return new, False, versioned


def _agency_segments(row: dict) -> tuple[list[str], list[str]]:
    """``CGAC.FPDS.AAC`` truncated at the first empty code, with the parallel names when all
    are present (otherwise entities are named by their code prefix)."""
    codes = [row["CGAC"].strip(), row["FPDS Code"].strip(), row["AAC Code"].strip()]
    names = [row["Department/Ind.Agency"].strip(), row["Sub-Tier"].strip(), row["Office"].strip()]
    depth = next((i for i, code in enumerate(codes) if not code), 3)
    codes, names = codes[:depth], names[:depth]
    return codes, names if all(names) else []


def _opt(cell: str | None) -> str | None:
    """The CSV has no null; an empty cell is its only 'absent'."""
    if cell is None:
        return None
    cell = cell.strip()
    return cell or None


def _file_key(path: Path, naics: list[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return f"{digest.hexdigest()}:{','.join(sorted(naics))}"


def _resume_offset(conn: sqlite3.Connection, key: str) -> int:
    row = conn.execute(
        "SELECT cursor FROM ingestion_runs WHERE source_id = ? AND cursor LIKE ? || ':%'"
        " ORDER BY run_id DESC LIMIT 1",
        (SOURCE_ID, key),
    ).fetchone()
    return int(row[0].rsplit(":", 1)[1]) if row else 0
