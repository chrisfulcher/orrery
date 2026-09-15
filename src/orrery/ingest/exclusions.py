"""SAM.gov exclusions from the daily public extract: who may not be awarded work.

The extract is a public file on the same service the contract-opportunity extracts come from
(`docs/notes/exclusions-probe.md`): no key, no quota, one zipped CSV a day named by the day
it was cut. Four rows in five are named individuals, and this adapter counts them and reads
nothing else of them (principle 8): an exclusion is only ever recorded against a contractor
the store already holds, matched by an exact Unique Entity ID or CAGE, and no entity and no
alias is ever created from this file. The six person columns are blanked in every record
that is stored, whatever its classification, and the stored record is built from the known
column list, so a column SAM.gov renames is dropped rather than kept.

The file holds active records only. An exclusion that has ended is therefore absent rather
than marked, which makes its absence from a later complete file an observation in its own
right: it becomes a ``terminated`` status fact at that file's own cut, with ``absence`` as
the extraction method, never a deleted row. A run that stopped early, was cancelled, or read
a file whose name does not say which day it was cut terminates nothing, because none of them
can tell "gone" from "not reached". Serves docs/DESIGN.md §5 source 3 and §8.
"""

import csv
import io
import json
import re
import sqlite3
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TextIO

import httpx

from orrery import db, runs
from orrery.config import Settings
from orrery.ingest import download
from orrery.progress import Cancelled, Report, check, never, quiet

SOURCE_ID = "sam_exclusions"
BATCH = 500  # matched rows per transaction

EXTRACT_URL = (
    "https://sam.gov/api/prod/fileextractservices/v1/api/download/"
    "Exclusions/Public%20V2/{name}?privacy=Public"
)

# What the termination pass did, so a pass that terminated nothing is never confused with one
# that never ran. Only ``TERMINATION_DONE`` makes ``terminated`` mean anything.
TERMINATION_DONE = "done"
TERMINATION_PARTIAL = "skipped: the pass did not reach the end of the file"
TERMINATION_NO_CUT = "skipped: the file name does not say which day it was cut"

COLUMNS = [
    "Classification", "Name", "Prefix", "First", "Middle", "Last", "Suffix",
    "Address 1", "Address 2", "Address 3", "Address 4", "City", "State / Province",
    "Country", "Zip Code", "Open Data Flag", "Blank (Deprecated)", "Unique Entity ID",
    "Exclusion Program", "Excluding Agency", "CT Code", "Exclusion Type",
    "Additional Comments", "Active Date", "Termination Date", "Record Status",
    "Cross-Reference", "SAM Number", "CAGE", "NPI", "Creation_Date",
]  # fmt: skip

PERSON_COLUMNS = frozenset({"Prefix", "First", "Middle", "Last", "Suffix", "NPI"})
"""The columns that describe a person rather than an organization. Blanked in every stored
record, whatever the row's classification (principle 8)."""

REQUIRED_COLUMNS = frozenset(
    {
        "Classification", "Name", "Unique Entity ID", "Exclusion Program", "Excluding Agency",
        "CT Code", "Exclusion Type", "Active Date", "Termination Date", "Record Status",
        "SAM Number", "CAGE",
    }
)  # fmt: skip
"""The columns the adapter reads. A file missing one of them is a file whose layout changed,
which is a failed run rather than a run that quietly records less."""

INDIVIDUAL = "Individual"

INSERT_FACT = (
    "INSERT INTO facts (subject_type, subject_id, predicate, value_type, value, source_id,"
    " source_ref, observed_at, confidence, extraction_method)"
    " VALUES ('entity', ?, ?, ?, ?, ?, ?, ?, 1.0, ?)"
)

# The latest value of each predicate for one exclusion, so a rerun writes nothing and a
# changed value writes exactly one fact.
LATEST_FOR_EXCLUSION = """
SELECT predicate, value FROM (
    SELECT predicate, value,
           row_number() OVER (PARTITION BY predicate ORDER BY observed_at DESC, fact_id DESC) AS rn
    FROM facts
    WHERE subject_type = 'entity' AND subject_id = ? AND source_id = ? AND source_ref = ?
      AND predicate LIKE 'sam.exclusion.%'
) WHERE rn = 1
"""

# Exclusions this store believes are active and that were last observed before this file was
# cut. Anything here the file no longer carries has ended.
ACTIVE_BEFORE_CUT = """
SELECT subject_id, source_ref FROM (
    SELECT subject_id, source_ref, value, observed_at,
           row_number() OVER (PARTITION BY subject_id, source_ref
                              ORDER BY observed_at DESC, fact_id DESC) AS rn
    FROM facts
    WHERE subject_type = 'entity' AND source_id = ?
      AND predicate = 'sam.exclusion.status' AND source_ref IS NOT NULL
) WHERE rn = 1 AND value = 'active' AND observed_at < ?
"""

_NAME_RE = re.compile(r"SAM_Exclusions_Public_Extract_V2_(\d{2})(\d{3})\.(?:zip|csv)$", re.I)


class ExclusionsError(Exception):
    """The extract could not be downloaded or is not the file we expect."""


@dataclass(frozen=True)
class ExclusionsResult:
    run_id: int
    rows_read: int
    rows_matched: int
    individuals_skipped: int
    exclusions_new: int
    facts_added: int
    terminated: int
    termination_pass: str = TERMINATION_NO_CUT


@dataclass(frozen=True)
class ExclusionRecord:
    """One non-individual exclusion row, with every person column already blank."""

    sam_number: str
    classification: str
    name: str
    uei: str | None
    cage: str | None
    exclusion_type: str | None
    program: str | None
    agency: str | None
    ct_code: str | None
    active_date: str | None
    termination_date: str | None
    """None when the record says ``Indefinite``: an exclusion with no end is not one that
    ended on an unknown day."""
    status: str
    record: dict
    """The row as stored: the known columns only, the person ones blank."""

    def facts(self) -> list[tuple[str, str, str]]:
        """(predicate, value, value_type) for every attribute present."""
        single = (
            ("sam.exclusion.status", self.status, "text"),
            ("sam.exclusion.type", self.exclusion_type, "text"),
            ("sam.exclusion.program", self.program, "text"),
            ("sam.exclusion.agency", self.agency, "text"),
            ("sam.exclusion.ct_code", self.ct_code, "text"),
            ("sam.exclusion.active_date", self.active_date, "date"),
            ("sam.exclusion.termination_date", self.termination_date, "date"),
        )
        facts = [(p, v, t) for p, v, t in single if v]
        facts.append(
            (
                "sam.exclusion.record",
                json.dumps(self.record, sort_keys=True, separators=(",", ":")),
                "text",
            )
        )
        return facts


def parse_row(row: dict) -> ExclusionRecord:
    """One row of the extract. An individual's row is refused rather than parsed: nothing
    about a named person is read past the classification that says they are one."""
    classification = (row.get("Classification") or "").strip()
    if classification == INDIVIDUAL:
        raise ExclusionsError("an individual's row is never parsed")
    record = {
        column: "" if column in PERSON_COLUMNS else (row.get(column) or "").strip()
        for column in COLUMNS
    }
    status = (record["Record Status"] or "").strip().lower() or "active"
    return ExclusionRecord(
        sam_number=record["SAM Number"],
        classification=classification,
        name=record["Name"],
        uei=record["Unique Entity ID"] or None,
        cage=record["CAGE"] or None,
        exclusion_type=record["Exclusion Type"] or None,
        program=record["Exclusion Program"] or None,
        agency=record["Excluding Agency"] or None,
        ct_code=record["CT Code"] or None,
        active_date=_date(record["Active Date"]),
        termination_date=_date(record["Termination Date"]),
        status=status,
        record=record,
    )


def _date(cell: str) -> str | None:
    """An ISO or MM/DD/YYYY date; blank and ``Indefinite`` are both absent."""
    cell = cell.strip()
    if not cell or cell.lower() == "indefinite":
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(cell, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def extract_name(day: date) -> str:
    """The file's name on the service: two-digit year and three-digit day of the year."""
    return f"SAM_Exclusions_Public_Extract_V2_{day:%y%j}.ZIP"


def file_date(path: Path) -> date | None:
    """The day the file was cut, from its name; None for a file the user named themselves."""
    match = _NAME_RE.search(path.name)
    if match is None:
        return None
    year, day_of_year = int(match.group(1)), int(match.group(2))
    try:
        return date(2000 + year, 1, 1) + timedelta(days=day_of_year - 1)
    except (ValueError, OverflowError):
        return None


def cut_of(path: Path) -> str | None:
    """When the file was cut, as the store writes timestamps; None when the name does not say."""
    day = file_date(path)
    return f"{day.isoformat()}T00:00:00Z" if day else None


def fetch_extract(
    dest_dir: Path, *, http: httpx.Client | None = None, report: Report = quiet
) -> download.Extract:
    """Today's extract in ``dest_dir``, else yesterday's.

    The daily file appears during its own UTC day, so early in that day today's name is not
    there yet and yesterday's is the current file. A name with no file behind it answers 204
    No Content rather than 404 (`docs/notes/exclusions-probe.md`), which the download treats
    as the failure it is. A copy already on disk under either name is that day's file and is
    reused: the name carries the date, so there is no staleness to reason about.
    """
    today = datetime.now(UTC).date()
    days = (today, today - timedelta(days=1))
    for day in days:
        dest = dest_dir / extract_name(day)
        if dest.exists():
            return download.read_meta(dest)
    failure: Exception | None = None
    for day in days:
        name = extract_name(day)
        try:
            return download.download(
                EXTRACT_URL.format(name=name),
                dest_dir / name,
                http=http,
                report=report,
                error=ExclusionsError,
            )
        except ExclusionsError as exc:
            failure = exc
            report(f"{name} is not published yet; trying the previous day")
    assert failure is not None
    raise failure


def wanted_keys(conn: sqlite3.Connection) -> tuple[dict[str, int], dict[str, int]]:
    """The contractors this store holds, by UEI and by CAGE. Only an exact match on one of
    these is read from the file; nothing in it ever creates an entity."""
    by_uei: dict[str, int] = {}
    by_cage: dict[str, int] = {}
    for entity_id, uei, cage in conn.execute(
        "SELECT entity_id, uei, cage FROM entities WHERE kind = 'contractor'"
    ):
        if uei:
            by_uei[uei.strip().upper()] = entity_id
        if cage:
            by_cage[cage.strip().upper()] = entity_id
    return by_uei, by_cage


def ingest_extract(
    conn: sqlite3.Connection,
    settings: Settings,
    path: Path,
    *,
    limit: int | None = None,
    report: Report = quiet,
    cancelled: Cancelled = never,
) -> ExclusionsResult:
    """Read one exclusions extract, writing facts for the contractors the store already has.

    ``limit`` caps matched rows this run and, like a cancellation or a file whose name does
    not date it, skips the termination pass and says so in ``termination_pass``. There is no
    resume cursor: the file parses in seconds, so a run that stopped is simply rerun.
    """
    cut = cut_of(path)
    observed_at = cut or db.utcnow()
    by_uei, by_cage = wanted_keys(conn)
    run_id = runs.start(conn, SOURCE_ID)
    conn.execute(
        "UPDATE ingestion_runs SET source_generated_at = ? WHERE run_id = ?", (cut, run_id)
    )
    read = matched = individuals = new = facts = terminated = 0
    seen: set[tuple[int, str]] = set()
    in_batch = 0
    complete = True
    termination_pass = TERMINATION_NO_CUT

    def commit_batch() -> None:
        if conn.in_transaction:
            conn.execute("COMMIT")
        report(f"{matched} exclusions matched, {read} rows read")

    try:
        csv.field_size_limit(1 << 24)
        with _open_extract(path) as handle:
            reader = csv.DictReader(handle)
            missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
            if missing:
                raise ExclusionsError(f"{path.name}: missing columns {sorted(missing)}")
            for row in reader:
                read += 1
                if (row.get("Classification") or "").strip() == INDIVIDUAL:
                    individuals += 1
                    continue
                entity_id = _match(row, by_uei, by_cage)
                if entity_id is None:
                    continue
                record = parse_row(row)
                if not record.sam_number:
                    continue  # nothing to key the exclusion by
                if in_batch == 0:
                    conn.execute("BEGIN")
                is_new, added = _apply(conn, entity_id, record, observed_at)
                matched += 1
                in_batch += 1
                new += is_new
                facts += added
                seen.add((entity_id, record.sam_number))
                if in_batch >= BATCH:
                    commit_batch()
                    in_batch = 0
                    check(cancelled)
                if limit is not None and matched >= limit:
                    complete = False
                    break
        commit_batch()
        if not complete:
            termination_pass = TERMINATION_PARTIAL
            report(f"termination pass {TERMINATION_PARTIAL}")
        elif cut is None:
            termination_pass = TERMINATION_NO_CUT
            report(f"termination pass {TERMINATION_NO_CUT}")
        else:
            termination_pass = TERMINATION_DONE
            terminated = _terminate(conn, cut, seen)
    except Exception as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        runs.finish(conn, run_id, status="failed", records_returned=matched, error=str(exc))
        raise
    if read and not matched:
        report("no exclusion in this file matches a contractor in the store")
    runs.finish(conn, run_id, status="succeeded", records_returned=matched)
    return ExclusionsResult(
        run_id, read, matched, individuals, new, facts, terminated, termination_pass
    )


def _match(row: dict, by_uei: dict[str, int], by_cage: dict[str, int]) -> int | None:
    """The contractor this row is about, by exact key only. UEI first: it is the key the
    store is built on, and a CAGE can move between registrations."""
    uei = (row.get("Unique Entity ID") or "").strip().upper()
    if uei and uei in by_uei:
        return by_uei[uei]
    cage = (row.get("CAGE") or "").strip().upper()
    if cage and cage in by_cage:
        return by_cage[cage]
    return None


def _apply(
    conn: sqlite3.Connection, entity_id: int, record: ExclusionRecord, observed_at: str
) -> tuple[bool, int]:
    """Write what this file says that the store does not already say. Returns
    (exclusion_new, facts_added)."""
    latest = dict(
        conn.execute(
            LATEST_FOR_EXCLUSION, (str(entity_id), SOURCE_ID, record.sam_number)
        ).fetchall()
    )
    changed = [
        (str(entity_id), predicate, value_type, value, SOURCE_ID, record.sam_number,
         observed_at, "extract")
        for predicate, value, value_type in record.facts()
        if latest.get(predicate) != value
    ]  # fmt: skip
    conn.executemany(INSERT_FACT, changed)
    return not latest, len(changed)


def _terminate(conn: sqlite3.Connection, cut: str, seen: set[tuple[int, str]]) -> int:
    """Every exclusion this store believes is active, observed before this file was cut, and
    absent from it. The file carries active records only, so its silence is the observation."""
    gone = [
        (subject_id, source_ref)
        for subject_id, source_ref in conn.execute(ACTIVE_BEFORE_CUT, (SOURCE_ID, cut)).fetchall()
        if (int(subject_id), source_ref) not in seen
    ]
    if not gone:
        return 0
    conn.execute("BEGIN")
    conn.executemany(
        INSERT_FACT,
        [
            (
                subject_id,
                "sam.exclusion.status",
                "text",
                "terminated",
                SOURCE_ID,
                source_ref,
                cut,
                "absence",
            )
            for subject_id, source_ref in gone
        ],
    )
    conn.execute("COMMIT")
    return len(gone)


@contextmanager
def _open_extract(path: Path) -> Iterator[TextIO]:
    """The single CSV member of the extract zip, or a bare CSV of the same layout."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
            if not names:
                raise ExclusionsError(f"{path.name}: no .csv member")
            with archive.open(names[0]) as member:
                yield io.TextIOWrapper(member, encoding="utf-8", errors="replace", newline="")
    else:
        with open(path, encoding="utf-8", errors="replace", newline="") as handle:
            yield handle
