"""Award history from USAspending: the configured NAICS slice for a date window.

One download request per run, prepared by the service and streamed as a zip; the award-level
CSV inside it becomes ``contracts`` rows. Each row resolves to an existing office entity by
its awarding office code alone (never creating agencies or offices: USAspending's top-tier
codes are not SAM.gov's), and to a contractor entity by UEI, created on first sight. What
cannot resolve is recorded as an unresolved alias and the contract is stored anyway.
Progress is committed in batches with a resume cursor keyed by the file's content and the
NAICS filter, as the bulk adapter does. Serves docs/DESIGN.md §5 source 4 and §8.
"""

import csv
import hashlib
import io
import json
import sqlite3
import time
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TextIO

import httpx

from orrery import db, naics, runs
from orrery.config import Settings
from orrery.ingest.notices import record_alias
from orrery.progress import Cancelled, Report, check, never, quiet
from orrery.usaspending.client import UsaspendingClient

SOURCE_ID = "usaspending_awards"
BATCH = 1000  # matched rows per transaction
AWARD_TYPES = ["A", "B", "C", "D"]  # contracts: BPA calls, purchase orders, orders, definitive
MEMBER_PREFIX = "Contracts_PrimeAwardSummaries"

REQUIRED_COLUMNS = frozenset(
    {
        "contract_award_unique_key", "award_id_piid", "parent_award_id_piid",
        "current_total_value_of_award", "potential_total_value_of_award",
        "award_base_action_date", "award_latest_action_date",
        "period_of_performance_start_date", "period_of_performance_current_end_date",
        "period_of_performance_potential_end_date",
        "awarding_office_code", "awarding_office_name", "recipient_uei", "recipient_name",
        "cage_code", "solicitation_identifier", "naics_code", "product_or_service_code",
        "award_type_code", "type_of_set_aside_code", "extent_competed_code",
    }
)  # fmt: skip
# Vendor employees are not officials in a public capacity (principle 8): never stored.
DROPPED_COLUMNS = frozenset(
    {"recipient_phone_number", "recipient_fax_number"}
    | {
        f"highly_compensated_officer_{i}_{field}"
        for i in range(1, 6)
        for field in ("name", "amount")
    }
)

UPSERT_CONTRACT = """
INSERT INTO contracts (
    award_key, piid, parent_piid, awarding_entity_id, awarding_office_code,
    awarding_office_name, vendor_entity_id, recipient_name, recipient_uei, cage,
    solicitation_identifier, award_date, last_action_date, pop_start, pop_end, value_usd,
    potential_value_usd, naics_code, psc_code, award_type_code, set_aside_code,
    extent_competed_code, source_id, first_seen_at, last_seen_at, raw_json, pop_potential_end
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(award_key) DO UPDATE SET
    piid = excluded.piid,
    parent_piid = excluded.parent_piid,
    awarding_entity_id = excluded.awarding_entity_id,
    awarding_office_code = excluded.awarding_office_code,
    awarding_office_name = excluded.awarding_office_name,
    vendor_entity_id = excluded.vendor_entity_id,
    recipient_name = excluded.recipient_name,
    recipient_uei = excluded.recipient_uei,
    cage = excluded.cage,
    solicitation_identifier = excluded.solicitation_identifier,
    award_date = excluded.award_date,
    last_action_date = excluded.last_action_date,
    pop_start = excluded.pop_start,
    pop_end = excluded.pop_end,
    value_usd = excluded.value_usd,
    potential_value_usd = excluded.potential_value_usd,
    naics_code = excluded.naics_code,
    psc_code = excluded.psc_code,
    award_type_code = excluded.award_type_code,
    set_aside_code = excluded.set_aside_code,
    extent_competed_code = excluded.extent_competed_code,
    last_seen_at = excluded.last_seen_at,
    raw_json = excluded.raw_json,
    pop_potential_end = excluded.pop_potential_end
"""


class AwardsError(Exception):
    """The file is not the award summary we expect."""


@dataclass(frozen=True)
class AwardsResult:
    run_id: int
    rows_read: int
    rows_matched: int
    contracts_new: int
    contracts_updated: int
    contractors_new: int
    offices_unresolved: int
    vendors_unresolved: int
    resumed_from: int


def award_filters(naics: list[str], *, since: date, until: date) -> dict:
    """The USAspending filter object for the slice: contracts in these NAICS codes whose
    actions fall in the window."""
    return {
        "time_period": [
            {
                "start_date": since.isoformat(),
                "end_date": until.isoformat(),
                "date_type": "action_date",
            }
        ],
        "award_type_codes": AWARD_TYPES,
        "naics_codes": {"require": list(naics)},
    }


def fetch_awards(
    settings: Settings,
    *,
    since: date,
    until: date,
    http: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    report: Report = quiet,
) -> Path:
    """Request, wait for, and download one slice into ``data_dir/extracts/usaspending``."""
    if not settings.naics:
        raise ValueError("no NAICS codes configured (ORRERY_NAICS)")
    with UsaspendingClient(settings, http, sleep=sleep) as client:
        ticket = client.request_awards_download(
            award_filters(settings.naics, since=since, until=until)
        )
        report(f"requested {ticket.file_name}; waiting for USAspending to prepare it")
        rows = client.wait_until_ready(ticket)
        report(f"ready: {rows} rows; downloading")
        return client.download(ticket, settings.data_dir / "extracts" / "usaspending")


def ingest_awards(
    conn: sqlite3.Connection,
    settings: Settings,
    path: Path,
    *,
    limit: int | None = None,
    since: date | None = None,
    until: date | None = None,
    report: Report = quiet,
    cancelled: Cancelled = never,
) -> AwardsResult:
    """Stream one award summary file (the zip, or its CSV), writing rows in the NAICS slice in
    batches of ``BATCH``. ``limit`` caps matched rows this run. ``since`` and ``until`` are the
    window the file was requested for, recorded on the run; an ingest from a file the user
    supplied has none. The run is closed as failed with the error on any exception; committed
    batches and their cursor stand."""
    if not settings.naics:
        raise ValueError("no NAICS codes configured (ORRERY_NAICS)")
    tally = naics.SliceTally(settings.naics)
    key = _file_key(path, settings.naics)
    resumed_from = _resume_offset(conn, key)
    run_id = runs.start(conn, SOURCE_ID, posted_from=since, posted_to=until)
    now = db.utcnow()
    read = matched = new = updated = contractors = offices_unresolved = vendors_unresolved = 0
    in_batch = 0
    position = resumed_from
    offices: dict[str, int | None] = {}

    def commit_batch() -> None:
        conn.execute(
            "UPDATE ingestion_runs SET cursor = ? WHERE run_id = ?", (f"{key}:{position}", run_id)
        )
        if conn.in_transaction:
            conn.execute("COMMIT")
        report(f"{matched} awards in slice, {read} rows read")

    try:
        csv.field_size_limit(1 << 24)
        with _open_summary(path) as rows:
            for position, row in enumerate(rows, 1):
                if position <= resumed_from:
                    continue
                read += 1
                if not tally.take(_opt(row["naics_code"])):
                    continue
                if in_batch == 0:
                    conn.execute("BEGIN")
                is_new, contractor_new, office_id, vendor_id = _ingest_row(conn, row, now, offices)
                matched += 1
                in_batch += 1
                new += is_new
                updated += not is_new
                contractors += contractor_new
                offices_unresolved += office_id is None
                vendors_unresolved += vendor_id is None
                if in_batch >= BATCH:
                    commit_batch()
                    in_batch = 0
                    check(cancelled)
                if limit is not None and matched >= limit:
                    break
        commit_batch()
    except Exception as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        runs.finish(
            conn, run_id, status="failed", records_returned=matched, error=str(exc),
            filter_json=tally.as_json(),
        )  # fmt: skip
        raise
    naics.report_empty(tally, report, rows_read=read)
    runs.finish(
        conn, run_id, status="succeeded", records_returned=matched, filter_json=tally.as_json()
    )
    return AwardsResult(
        run_id, read, matched, new, updated, contractors, offices_unresolved, vendors_unresolved,
        resumed_from,
    )  # fmt: skip


def resolve_office(
    conn: sqlite3.Connection, code: str | None, name: str | None, source_id: str, now: str
) -> int | None:
    """The office entity whose agency path ends with ``.<code>``.

    One candidate resolves. Several that share the same first two segments are the deep and
    shallow twins of one office (the bulk extract cannot reproduce intermediate segments), and
    the longest path wins. Anything else stays unresolved and ``name`` joins the alias queue.
    Never creates an entity: the office code alone cannot place one in the hierarchy.
    """
    if not code:
        return None
    candidates = conn.execute(
        "SELECT entity_id, agency_path_code FROM entities WHERE kind = 'office'"
        " AND substr(agency_path_code, -length(?) - 1) = '.' || ?",
        (code, code),
    ).fetchall()
    entity_id: int | None = None
    if len(candidates) == 1:
        entity_id = candidates[0][0]
    elif len(candidates) > 1:
        roots = {".".join(path.split(".")[:2]) for _, path in candidates}
        if len(roots) == 1:
            entity_id = max(candidates, key=lambda c: len(c[1]))[0]
    if entity_id is None:
        if name:
            record_unresolved(conn, name, source_id)
        return None
    if name:
        record_alias(conn, entity_id, name, source_id, now)
    return entity_id


def resolve_contractor(
    conn: sqlite3.Connection,
    uei: str | None,
    cage: str | None,
    name: str | None,
    source_id: str,
    now: str,
) -> tuple[int | None, bool]:
    """The contractor entity with this UEI, created on first sight. Returns (id, created).

    The CAGE is set only while no other entity holds it (the store is keyed by UEI; a CAGE
    that moves between registrations is left for a resolver). Without a UEI nothing
    resolves; the recipient name joins the alias queue.
    """
    if not uei:
        if name:
            record_unresolved(conn, name, source_id)
        return None, False
    row = conn.execute("SELECT entity_id, cage FROM entities WHERE uei = ?", (uei,)).fetchone()
    created = row is None
    if created:
        (entity_id,) = conn.execute(
            "INSERT INTO entities (kind, name, uei, cage, source_id, first_seen_at, last_seen_at)"
            " VALUES ('contractor', ?, ?, ?, ?, ?, ?) RETURNING entity_id",
            (name or uei, uei, _free_cage(conn, cage), source_id, now, now),
        ).fetchone()
    else:
        entity_id = row[0]
        conn.execute("UPDATE entities SET last_seen_at = ? WHERE entity_id = ?", (now, entity_id))
        if cage and row[1] is None and _free_cage(conn, cage):
            conn.execute("UPDATE entities SET cage = ? WHERE entity_id = ?", (cage, entity_id))
    if name:
        record_alias(conn, entity_id, name, source_id, now)
    return entity_id, created


def record_unresolved(conn: sqlite3.Connection, alias: str, source_id: str) -> None:
    """Queue a name no entity claims, once per source."""
    conn.execute(
        "INSERT INTO entity_aliases (alias, source_id) SELECT ?, ? WHERE NOT EXISTS"
        " (SELECT 1 FROM entity_aliases WHERE alias = ? AND source_id = ? AND entity_id IS NULL)",
        (alias, source_id, alias, source_id),
    )


def _ingest_row(
    conn: sqlite3.Connection, row: dict, now: str, offices: dict[str, int | None]
) -> tuple[bool, bool, int | None, int | None]:
    """Write one award. Returns (new, contractor_created, office_id, vendor_id)."""
    award_key = row["contract_award_unique_key"].strip()
    code = _opt(row["awarding_office_code"])
    if code not in offices:
        offices[code] = resolve_office(
            conn, code, _opt(row["awarding_office_name"]), SOURCE_ID, now
        )
    office_id = offices[code]
    vendor_id, created = resolve_contractor(
        conn, _opt(row["recipient_uei"]), _opt(row["cage_code"]), _opt(row["recipient_name"]),
        SOURCE_ID, now,
    )  # fmt: skip
    existing = conn.execute("SELECT 1 FROM contracts WHERE award_key = ?", (award_key,)).fetchone()
    raw = {k: v for k, v in row.items() if k not in DROPPED_COLUMNS and k != "_extra"}
    conn.execute(
        UPSERT_CONTRACT,
        (
            award_key,
            row["award_id_piid"].strip(),
            _opt(row["parent_award_id_piid"]),
            office_id,
            code,
            _opt(row["awarding_office_name"]),
            vendor_id,
            _opt(row["recipient_name"]),
            _opt(row["recipient_uei"]),
            _opt(row["cage_code"]),
            _opt(row["solicitation_identifier"]),
            _opt(row["award_base_action_date"]),
            _opt(row["award_latest_action_date"]),
            _opt(row["period_of_performance_start_date"]),
            _opt(row["period_of_performance_current_end_date"]),
            _num(row["current_total_value_of_award"]),
            _num(row["potential_total_value_of_award"]),
            _opt(row["naics_code"]),
            _opt(row["product_or_service_code"]),
            _opt(row["award_type_code"]),
            _opt(row["type_of_set_aside_code"]),
            _opt(row["extent_competed_code"]),
            SOURCE_ID,
            now,
            now,
            json.dumps(raw, sort_keys=True, separators=(",", ":")),
            _date(row["period_of_performance_potential_end_date"]),
        ),
    )
    return existing is None, created, office_id, vendor_id


def _member_order(name: str) -> tuple[int, str]:
    """Members sort by their trailing number, not lexicographically: ``_10`` would otherwise
    come before ``_2`` and the rows would be read out of order, which the resume cursor -- a
    row offset across the whole file -- cannot survive."""
    stem = Path(name).stem
    tail = stem.rsplit("_", 1)[-1]
    return (int(tail) if tail.isdigit() else 0, stem)


def _member_rows(handle: TextIO, label: str) -> Iterator[dict]:
    """One CSV member. Every member carries its own header, so every member is checked."""
    reader = csv.DictReader(handle, restkey="_extra")
    missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
    if missing:
        raise AwardsError(f"{label}: missing columns {sorted(missing)}")
    yield from reader


@contextmanager
def _open_summary(path: Path) -> Iterator[Iterator[dict]]:
    """The award-summary rows in order, across every member of the zip. USAspending splits a
    large download into numbered members, so reading only the first silently stops at its end
    -- invisible until a slice is wide enough to split the file, which is exactly what prefix
    matching makes reachable."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names = sorted(
                (n for n in archive.namelist() if Path(n).name.startswith(MEMBER_PREFIX)),
                key=_member_order,
            )
            if not names:
                raise AwardsError(f"{path.name}: no {MEMBER_PREFIX}*.csv member")

            def rows() -> Iterator[dict]:
                for name in names:
                    with archive.open(name) as member:
                        wrapper = io.TextIOWrapper(member, encoding="utf-8-sig", newline="")
                        yield from _member_rows(wrapper, f"{path.name}:{Path(name).name}")

            yield rows()
    else:
        with open(path, encoding="utf-8-sig", newline="") as handle:
            yield _member_rows(handle, path.name)


def _free_cage(conn: sqlite3.Connection, cage: str | None) -> str | None:
    if not cage:
        return None
    taken = conn.execute("SELECT 1 FROM entities WHERE cage = ?", (cage,)).fetchone()
    return None if taken else cage


def _opt(cell: str | None) -> str | None:
    if cell is None:
        return None
    cell = cell.strip()
    return cell or None


def _date(cell: str | None) -> str | None:
    """The date part of a source timestamp such as '2027-02-28 00:00:00'."""
    value = _opt(cell)
    return value[:10] if value else None


def _num(cell: str | None) -> float | None:
    value = _opt(cell)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


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
