import json
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import EXTRACT_SAMPLE
from pytest_httpx import HTTPXMock

from orrery import db
from orrery.config import Settings
from orrery.ingest import entities
from orrery.ingest.awards import AwardsResult
from orrery.ingest.entities import (
    EntitiesError,
    ingest_extract,
    lookup_entities,
    parse_api_record,
    parse_extract_row,
)

SeedAwards = Callable[[list[dict] | None], AwardsResult]
FIXTURES = Path(__file__).with_name("fixtures")
LEIDOS, CDW, UNIVERSITY = "UE9QJD4KK1L6", "PHZDZ8SJ5CM1", "C39LJA3KD378"


def records() -> list[list[str]]:
    lines = EXTRACT_SAMPLE.read_text(encoding="utf-8").split("\n")
    return [line.split("|") for line in lines[1:4]]


def write_extract(tmp_path: Path, rows: list[list[str]], name: str = "extract.dat") -> Path:
    head = "BOF PUBLIC V2 00000000 20260906 0000003 0000003\n"
    body = "".join("|".join(row) + "\n" for row in rows)
    path = tmp_path / name
    path.write_text(head + body + head.replace("BOF", "EOF"), encoding="utf-8")
    return path


def facts(conn: sqlite3.Connection, uei: str, predicate: str) -> list[tuple[str, str]]:
    return conn.execute(
        "SELECT f.value, f.observed_at FROM facts f JOIN entities e ON e.entity_id = f.subject_id"
        " WHERE e.uei = ? AND f.predicate = ? ORDER BY f.fact_id",
        (uei, predicate),
    ).fetchall()


def test_parse_extract_row_types_the_record_and_drops_contacts() -> None:
    leidos, cdw, university = (parse_extract_row(row) for row in records())
    assert (leidos.uei, leidos.cage, leidos.legal_name) == (LEIDOS, "5UTE1", "LEIDOS, INC.")
    assert (leidos.status, leidos.expires, leidos.purpose) == ("Active", "2027-04-17", "Z2")
    assert leidos.structure == "2L" and leidos.business_types == ("2X", "MF")
    assert leidos.naics_primary == "541715" and leidos.naics[:2] == ("236220", "332311")
    assert all(len(code) == 6 and code.isdigit() for code in cdw.naics)  # flags stripped
    assert leidos.address is not None and "RESTON VA" in leidos.address
    assert university.status == "Expired" and university.naics_primary == "611310"
    assert leidos.record["layout"] == "SAM_PUBLIC_V2"
    assert all(field == "" for field in leidos.record["fields"][46:112])
    with pytest.raises(EntitiesError):
        parse_extract_row(["x"] * 10)
    predicates = {p for p, _, _ in leidos.facts()}
    assert {"sam.registration_status", "sam.naics", "sam.business_type"} <= predicates


def test_parse_api_record_matches_the_extract_parse() -> None:
    entity = json.loads((FIXTURES / "sam_entity_v3.json").read_text())["entityData"][0]
    api = parse_api_record(entity)
    extract = parse_extract_row(records()[0])
    assert (api.uei, api.cage, api.legal_name) == (extract.uei, extract.cage, extract.legal_name)
    assert (api.status, api.expires, api.structure) == ("Active", "2027-04-17", "2L")
    assert api.naics_primary == extract.naics_primary and set(api.naics) == set(extract.naics)
    assert "pointsOfContact" not in api.record and "Placeholder" not in json.dumps(api.record)


def test_extract_keeps_known_vendors_and_the_naics_slice(
    conn: sqlite3.Connection, settings: Settings, seed_awards: SeedAwards
) -> None:
    seed_awards()  # the two vendors exist as contractors from their awards
    result = ingest_extract(conn, settings, EXTRACT_SAMPLE)
    assert (result.rows_read, result.rows_matched, result.entities_new) == (3, 2, 0)
    assert (result.registrations_added, result.rows_malformed) == (2, 0)
    assert facts(conn, LEIDOS, "sam.registration_status") == [("Active", db.utcnow())]
    assert facts(conn, CDW, "sam.naics_primary") == [("423430", db.utcnow())]
    assert conn.execute(
        "SELECT count(*) FROM entities WHERE uei = ?", (UNIVERSITY,)
    ).fetchone() == (0,)
    (status, records_returned) = conn.execute(
        "SELECT status, records_returned FROM ingestion_runs WHERE run_id = ?", (result.run_id,)
    ).fetchone()
    assert (status, records_returned) == ("succeeded", 2)

    slice_settings = settings.model_copy(update={"naics": ["611310"]})
    result = ingest_extract(conn, slice_settings, EXTRACT_SAMPLE)
    assert (result.rows_matched, result.entities_new) == (3, 1)
    (kind, name) = conn.execute(
        "SELECT kind, name FROM entities WHERE uei = ?", (UNIVERSITY,)
    ).fetchone()
    assert kind == "contractor" and name.startswith("UNIVERSITY OF")
    assert facts(conn, UNIVERSITY, "sam.registration_status")[0][0] == "Expired"
    assert result.registrations_added == 1  # the two known vendors were unchanged


def test_unchanged_records_add_nothing_and_changes_append(
    conn: sqlite3.Connection,
    settings: Settings,
    seed_awards: SeedAwards,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_awards()
    rows = records()
    monkeypatch.setattr(db, "utcnow", lambda: "2026-09-01T00:00:00Z")
    ingest_extract(conn, settings, write_extract(tmp_path, rows, "first.dat"))
    monkeypatch.setattr(db, "utcnow", lambda: "2026-10-01T00:00:00Z")
    again = ingest_extract(conn, settings, write_extract(tmp_path, rows + [rows[1]], "same.dat"))
    assert (again.rows_matched, again.registrations_added, again.facts_added) == (3, 0, 0)

    rows[0][entities.EXTRACT_CODE] = "E"
    changed = ingest_extract(conn, settings, write_extract(tmp_path, rows, "changed.dat"))
    assert changed.registrations_added == 1
    assert facts(conn, LEIDOS, "sam.registration_status") == [
        ("Active", "2026-09-01T00:00:00Z"),
        ("Expired", "2026-10-01T00:00:00Z"),
    ]
    assert conn.execute(
        "SELECT count(*) FROM entity_registrations WHERE entity_id ="
        " (SELECT entity_id FROM entities WHERE uei = ?)",
        (LEIDOS,),
    ).fetchone() == (2,)


def test_malformed_records_are_counted_not_guessed(
    conn: sqlite3.Connection, settings: Settings, seed_awards: SeedAwards, tmp_path: Path
) -> None:
    seed_awards()
    rows = records()
    rows[0][entities.DBA_NAME] = "PIPE | IN | NAME"
    result = ingest_extract(conn, settings, write_extract(tmp_path, rows))
    assert (result.rows_read, result.rows_malformed, result.rows_matched) == (3, 1, 1)


def test_resume_and_limit(
    conn: sqlite3.Connection, settings: Settings, seed_awards: SeedAwards
) -> None:
    seed_awards()
    first = ingest_extract(conn, settings, EXTRACT_SAMPLE, limit=1)
    assert (first.rows_matched, first.resumed_from) == (1, 0)
    second = ingest_extract(conn, settings, EXTRACT_SAMPLE)
    assert (second.rows_matched, second.resumed_from) == (1, 2)


def test_zip_member_and_dba_alias(
    conn: sqlite3.Connection, settings: Settings, seed_awards: SeedAwards, tmp_path: Path
) -> None:
    import zipfile

    seed_awards()
    rows = records()
    rows[0][entities.DBA_NAME] = "LEIDOS"
    text = write_extract(tmp_path, rows).read_text(encoding="utf-8")
    archive = tmp_path / "SAM_PUBLIC_UTF-8_MONTHLY_V2_20260906.ZIP"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("SAM_PUBLIC_UTF-8_MONTHLY_V2_20260906.dat", text)
    ingest_extract(conn, settings, archive)
    aliases = [
        a
        for (a,) in conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id ="
            " (SELECT entity_id FROM entities WHERE uei = ?) ORDER BY alias",
            (LEIDOS,),
        )
    ]
    assert aliases == ["LEIDOS", "LEIDOS, INC."]
    assert entities.newest_extract(tmp_path) == archive


def test_lookup_creates_the_contractor_and_spends_one_request(
    conn: sqlite3.Connection, settings: Settings, httpx_mock: HTTPXMock
) -> None:
    fixture = json.loads((FIXTURES / "sam_entity_v3.json").read_text())
    httpx_mock.add_response(url=re.compile(r".*/entity-information/v3/entities.*"), json=fixture)
    result = lookup_entities(conn, settings, ["ue9qjd4kk1l6 "])
    assert (result.rows_read, result.entities_new, result.registrations_added) == (1, 1, 1)
    assert result.requests_spent == 1 and result.facts_added > 5
    (name, cage) = conn.execute(
        "SELECT name, cage FROM entities WHERE uei = ?", (LEIDOS,)
    ).fetchone()
    assert (name, cage) == ("LEIDOS, INC.", "5UTE1")
    stored = conn.execute("SELECT raw_json, source_ref FROM entity_registrations").fetchone()
    assert stored[1] == "api:v3" and "Placeholder" not in stored[0]
    assert conn.execute(
        "SELECT count(*) FROM facts WHERE value LIKE '%Placeholder%'"
    ).fetchone() == (0,)


def test_cancel_between_batches_and_lookup_progress(
    conn: sqlite3.Connection,
    settings: Settings,
    seed_awards: SeedAwards,
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
) -> None:
    from orrery.progress import JobCancelled

    seed_awards()
    monkeypatch.setattr(entities, "BATCH", 1)
    lines: list[str] = []
    with pytest.raises(JobCancelled):
        ingest_extract(
            conn, settings, EXTRACT_SAMPLE, report=lines.append, cancelled=lambda: len(lines) >= 1
        )
    assert lines == ["1 registrants in slice, 1 read"]
    assert conn.execute("SELECT count(*) FROM entity_registrations").fetchone() == (1,)
    assert ingest_extract(conn, settings, EXTRACT_SAMPLE).resumed_from == 2  # BOF is line 1

    fixture = json.loads((FIXTURES / "sam_entity_v3.json").read_text())
    httpx_mock.add_response(url=re.compile(r".*/entity-information/v3/entities.*"), json=fixture)
    lines = []
    lookup_entities(conn, settings, [LEIDOS], report=lines.append)
    assert lines == ["looked up 1 of 1"]
    with pytest.raises(JobCancelled):
        lookup_entities(conn, settings, [LEIDOS], cancelled=lambda: True)


def test_a_prefix_slice_takes_the_whole_industry_group(
    conn: sqlite3.Connection, settings: Settings, seed_awards: SeedAwards
) -> None:
    seed_awards()
    wide = settings.model_copy(update={"naics": ["6113"]})
    result = ingest_extract(conn, wide, EXTRACT_SAMPLE)
    assert (result.rows_matched, result.entities_new) == (3, 1)
    assert conn.execute(
        "SELECT count(*) FROM entities WHERE uei = ?", (UNIVERSITY,)
    ).fetchone() == (1,)
