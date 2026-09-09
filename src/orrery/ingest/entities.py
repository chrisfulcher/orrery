"""Contractor registrations from SAM.gov: the public monthly extract, or single lookups.

The extract is one keyed request for every registrant; only the slice is read from it:
registrants already in the graph (contractors seen in awards), registrants whose primary
NAICS is configured, and the user's own company. ``orrery ingest entities --uei`` looks up
a few registrants through the Entity Management API instead, ten per keyed request.

Either way a registration becomes: the contractor entity (created on first sight, keyed by
UEI, its names as aliases), one ``entity_registrations`` row holding the public record with
every point-of-contact field removed, written only when the record changed, and facts under
``sam.*`` predicates for its typed attributes. Registrant contacts are vendor employees and
are never stored (principle 8). Serves docs/DESIGN.md §5 source 3 and §8.
"""

import hashlib
import io
import json
import sqlite3
import zipfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from orrery import db, naics, runs, workspace
from orrery.config import Settings
from orrery.ingest.awards import resolve_contractor
from orrery.ingest.notices import record_alias
from orrery.progress import Cancelled, Report, check, never, quiet
from orrery.sam.client import ENTITIES_PER_REQUEST, SamClient

SOURCE_ID = "sam_entities"
BATCH = 500
FIELD_COUNT = 142  # Public V2 extract layout; the last field is the '!end' terminator

# 0-based positions in the pipe-delimited record (layout field number minus one).
UEI, CAGE, EXTRACT_CODE, PURPOSE, EXPIRES = 0, 3, 5, 6, 8
LEGAL_NAME, DBA_NAME = 11, 12
ADDRESS_1, ADDRESS_2, CITY, STATE, ZIP, ZIP4, COUNTRY = 15, 16, 17, 18, 19, 20, 21
STRUCTURE, BUSINESS_TYPES, NAICS_PRIMARY, NAICS_LIST, PSC_LIST = 27, 31, 32, 34, 36
POC_FIRST, POC_END = 46, 112  # six point-of-contact groups, blanked before storage
SBA_TYPES = 117

STATUS_BY_CODE = {"A": "Active", "E": "Expired", "1": "Deactivated", "2": "Active",
                  "3": "Active", "4": "Expired"}  # fmt: skip


@dataclass(frozen=True)
class Registration:
    uei: str
    cage: str | None
    legal_name: str
    dba_name: str | None
    status: str | None
    expires: str | None
    purpose: str | None
    structure: str | None
    business_types: tuple[str, ...]
    sba_types: tuple[str, ...]
    naics_primary: str | None
    naics: tuple[str, ...]
    psc: tuple[str, ...]
    address: str | None
    record: dict
    """The public record with every point-of-contact field removed; stored as raw_json."""

    def facts(self) -> list[tuple[str, str, str]]:
        """(predicate, value, value_type) for every typed attribute present."""
        single = (
            ("sam.legal_business_name", self.legal_name, "text"),
            ("sam.dba_name", self.dba_name, "text"),
            ("sam.registration_status", self.status, "text"),
            ("sam.registration_expires", self.expires, "date"),
            ("sam.purpose_of_registration", self.purpose, "text"),
            ("sam.entity_structure", self.structure, "text"),
            ("sam.naics_primary", self.naics_primary, "text"),
            ("sam.physical_address", self.address, "text"),
        )
        lists = (
            ("sam.business_type", self.business_types),
            ("sam.sba_business_type", self.sba_types),
            ("sam.naics", self.naics),
            ("sam.psc", self.psc),
        )
        facts = [(p, v, t) for p, v, t in single if v]
        facts += [(p, v, "text") for p, values in lists for v in values]
        return facts


@dataclass(frozen=True)
class EntitiesResult:
    run_id: int
    rows_read: int
    rows_matched: int
    rows_malformed: int
    entities_new: int
    registrations_added: int
    facts_added: int
    requests_spent: int
    resumed_from: int


class EntitiesError(Exception):
    """The file is not the public entity extract we expect."""


def parse_extract_row(fields: list[str]) -> Registration:
    """One 142-field record of the Public V2 extract."""
    if len(fields) != FIELD_COUNT:
        raise EntitiesError(f"expected {FIELD_COUNT} fields, got {len(fields)}")
    fields = [f.strip() for f in fields]
    for index in range(POC_FIRST, POC_END):
        fields[index] = ""
    expires = fields[EXPIRES]
    naics_codes = []
    for entry in _list(fields[NAICS_LIST]):
        code = entry[:6]
        if code.isdigit() and code not in naics_codes:
            naics_codes.append(code)
    return Registration(
        uei=fields[UEI],
        cage=fields[CAGE] or None,
        legal_name=fields[LEGAL_NAME] or fields[UEI],
        dba_name=fields[DBA_NAME] or None,
        status=STATUS_BY_CODE.get(fields[EXTRACT_CODE], fields[EXTRACT_CODE] or None),
        expires=f"{expires[:4]}-{expires[4:6]}-{expires[6:]}" if len(expires) == 8 else None,
        purpose=fields[PURPOSE] or None,
        structure=fields[STRUCTURE] or None,
        business_types=tuple(_list(fields[BUSINESS_TYPES])),
        sba_types=tuple(_list(fields[SBA_TYPES])),
        naics_primary=fields[NAICS_PRIMARY] or None,
        naics=tuple(naics_codes),
        psc=tuple(_list(fields[PSC_LIST])),
        address=_address(
            fields[ADDRESS_1],
            fields[ADDRESS_2],
            fields[CITY],
            fields[STATE],
            fields[ZIP],
            fields[ZIP4],
            fields[COUNTRY],
        ),  # fmt: skip
        record={"layout": "SAM_PUBLIC_V2", "fields": fields},
    )


def parse_api_record(entity: dict) -> Registration:
    """One ``entityData`` element of the Entity Management API v3 response."""
    registration = entity.get("entityRegistration") or {}
    core = entity.get("coreData") or {}
    general = core.get("generalInformation") or {}
    address = core.get("physicalAddress") or {}
    types = core.get("businessTypes") or {}
    goods = (entity.get("assertions") or {}).get("goodsAndServices") or {}
    record = {key: value for key, value in entity.items() if key != "pointsOfContact"}
    uei = registration.get("ueiSAM") or ""
    naics_codes = []
    for item in goods.get("naicsList") or []:
        code = item.get("naicsCode")
        if code and code not in naics_codes:
            naics_codes.append(code)
    return Registration(
        uei=uei,
        cage=registration.get("cageCode") or None,
        legal_name=registration.get("legalBusinessName") or uei,
        dba_name=registration.get("dbaName") or None,
        status=registration.get("registrationStatus") or None,
        expires=registration.get("registrationExpirationDate") or None,
        purpose=registration.get("purposeOfRegistrationCode") or None,
        structure=general.get("entityStructureCode") or None,
        business_types=tuple(
            code
            for item in types.get("businessTypeList") or []
            if (code := item.get("businessTypeCode"))
        ),  # fmt: skip
        sba_types=tuple(
            code
            for item in types.get("sbaBusinessTypeList") or []
            if (code := item.get("sbaBusinessTypeCode"))
        ),  # fmt: skip
        naics_primary=goods.get("primaryNaics") or None,
        naics=tuple(naics_codes),
        psc=tuple(code for item in goods.get("pscList") or [] if (code := item.get("pscCode"))),
        address=_address(
            address.get("addressLine1"),
            address.get("addressLine2"),
            address.get("city"),
            address.get("stateOrProvinceCode"),
            address.get("zipCode"),
            address.get("zipCodePlus4"),
            address.get("countryCode"),
        ),  # fmt: skip
        record=record,
    )


def wanted_ueis(conn: sqlite3.Connection) -> set[str]:
    """Registrants the graph already cares about: every contractor, and the user's company."""
    ueis = {uei for (uei,) in conn.execute("SELECT uei FROM entities WHERE uei IS NOT NULL")}
    profile = workspace.get_profile(conn)
    if profile and profile.uei:
        ueis.add(profile.uei)
    return ueis


def fetch_extract(
    conn: sqlite3.Connection, settings: Settings, dest_dir: Path, *, report: Report = quiet
) -> Path:
    """Download the monthly extract under its own run: one keyed request."""
    run_id = runs.start(conn, SOURCE_ID)
    report("downloading the SAM.gov public monthly entity extract (one keyed request)")
    try:
        with SamClient(settings, conn, run_id) as sam:
            path = sam.download_entity_extract(dest_dir)
    except Exception as exc:
        runs.finish(conn, run_id, status="failed", error=str(exc))
        raise
    runs.finish(conn, run_id, status="succeeded", records_returned=0)
    return path


def newest_extract(dest_dir: Path) -> Path | None:
    files = sorted(dest_dir.glob("SAM_PUBLIC*.ZIP")) + sorted(dest_dir.glob("SAM_PUBLIC*.zip"))
    return max(files, key=lambda p: p.name) if files else None


def ingest_extract(
    conn: sqlite3.Connection,
    settings: Settings,
    path: Path,
    *,
    limit: int | None = None,
    report: Report = quiet,
    cancelled: Cancelled = never,
) -> EntitiesResult:
    """Stream one extract, keeping the slice, in batches of ``BATCH`` records. Resumable
    through the run cursor, as the bulk adapter is."""
    tally = naics.SliceTally(settings.naics)
    key = _file_key(path, settings.naics)
    resumed_from = _resume_offset(conn, key)
    wanted = wanted_ueis(conn)
    run_id = runs.start(conn, SOURCE_ID)
    now = db.utcnow()
    read = matched = malformed = new = registrations = facts = 0
    in_batch = 0
    position = resumed_from

    def commit_batch() -> None:
        conn.execute(
            "UPDATE ingestion_runs SET cursor = ? WHERE run_id = ?", (f"{key}:{position}", run_id)
        )
        if conn.in_transaction:
            conn.execute("COMMIT")
        report(f"{matched} registrants in slice, {read} read")

    try:
        with _open_extract(path) as handle:
            for position, line in enumerate(handle, 1):
                if position <= resumed_from or line.startswith(("BOF ", "EOF ")):
                    continue
                read += 1
                fields = line.rstrip("\n").split("|")
                if len(fields) != FIELD_COUNT or fields[-1] != "!end":
                    malformed += 1
                    continue
                uei, primary = fields[UEI].strip(), fields[NAICS_PRIMARY].strip()
                if uei not in wanted and not tally.take(primary):
                    continue
                if in_batch == 0:
                    conn.execute("BEGIN")
                created, added, fact_count = _apply(conn, parse_extract_row(fields), path.name, now)
                matched += 1
                in_batch += 1
                new += created
                registrations += added
                facts += fact_count
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
    return EntitiesResult(
        run_id, read, matched, malformed, new, registrations, facts, 0, resumed_from
    )


def lookup_entities(
    conn: sqlite3.Connection,
    settings: Settings,
    ueis: Iterable[str],
    *,
    report: Report = quiet,
    cancelled: Cancelled = never,
) -> EntitiesResult:
    """Fetch and apply the registrations of specific UEIs, ten per keyed request."""
    ueis = sorted({uei.strip().upper() for uei in ueis if uei.strip()})
    run_id = runs.start(conn, SOURCE_ID)
    now = db.utcnow()
    read = new = registrations = facts = 0
    try:
        with SamClient(settings, conn, run_id) as sam:
            for start in range(0, len(ueis), ENTITIES_PER_REQUEST):
                check(cancelled)
                body = sam.get_entities(ueis[start : start + ENTITIES_PER_REQUEST])
                report(f"looked up {min(start + ENTITIES_PER_REQUEST, len(ueis))} of {len(ueis)}")
                conn.execute("BEGIN")
                for entity in body.get("entityData") or []:
                    registration = parse_api_record(entity)
                    if not registration.uei:
                        continue
                    created, added, fact_count = _apply(conn, registration, "api:v3", now)
                    read += 1
                    new += created
                    registrations += added
                    facts += fact_count
                conn.execute("COMMIT")
    except Exception as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        runs.finish(conn, run_id, status="failed", records_returned=read, error=str(exc))
        raise
    runs.finish(conn, run_id, status="succeeded", records_returned=read)
    (spent,) = conn.execute(
        "SELECT requests_spent FROM ingestion_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    return EntitiesResult(run_id, read, read, 0, new, registrations, facts, spent, 0)


def _apply(
    conn: sqlite3.Connection, registration: Registration, source_ref: str, now: str
) -> tuple[bool, bool, int]:
    """Entity, names, and (when the record changed) one registration row plus its facts.
    Returns (entity_created, registration_added, facts_added)."""
    entity_id, created = resolve_contractor(
        conn, registration.uei, registration.cage, registration.legal_name, SOURCE_ID, now
    )
    assert entity_id is not None  # the UEI is always present here
    if registration.dba_name:
        record_alias(conn, entity_id, registration.dba_name, SOURCE_ID, now)
    raw_json = json.dumps(registration.record, sort_keys=True, separators=(",", ":"))
    raw_hash = hashlib.sha256(raw_json.encode()).hexdigest()
    latest = conn.execute(
        "SELECT raw_hash FROM entity_registrations WHERE entity_id = ?"
        " ORDER BY registration_id DESC LIMIT 1",
        (entity_id,),
    ).fetchone()
    if latest is not None and latest[0] == raw_hash:
        return created, False, 0
    conn.execute(
        "INSERT INTO entity_registrations (entity_id, source_id, source_ref, observed_at,"
        " raw_hash, raw_json) VALUES (?, ?, ?, ?, ?, ?)",
        (entity_id, SOURCE_ID, source_ref, now, raw_hash, raw_json),
    )
    facts = registration.facts()
    conn.executemany(
        "INSERT INTO facts (subject_type, subject_id, predicate, value_type, value, source_id,"
        " source_ref, observed_at, confidence, extraction_method)"
        " VALUES ('entity', ?, ?, ?, ?, ?, ?, ?, 1.0, 'parse')",
        [
            (str(entity_id), predicate, value_type, value, SOURCE_ID, source_ref, now)
            for predicate, value, value_type in facts
        ],
    )
    return created, True, len(facts)


@contextmanager
def _open_extract(path: Path) -> Iterator[TextIO]:
    """The .dat member of the extract zip, or a bare text file of the same layout."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith(".dat")]
            if not names:
                raise EntitiesError(f"{path.name}: no .dat member")
            with archive.open(names[0]) as member:
                yield io.TextIOWrapper(member, encoding="utf-8", errors="replace", newline="\n")
    else:
        with open(path, encoding="utf-8", errors="replace", newline="\n") as handle:
            yield handle


def _list(cell: str) -> list[str]:
    return [item.strip() for item in cell.split("~") if item.strip()]


def _address(
    line1: str | None, line2: str | None, city: str | None, state: str | None,
    zipcode: str | None, zip4: str | None, country: str | None,
) -> str | None:  # fmt: skip
    street = ", ".join(part for part in (line1, line2) if part)
    postal = f"{zipcode}-{zip4}" if zipcode and zip4 else zipcode
    locality = " ".join(part for part in (city, state, postal, country) if part)
    parts = [part for part in (street, locality) if part]
    return ", ".join(parts) if parts else None


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
