import json
import re
import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest
from conftest import make_awards_csv, make_awards_zip, make_extract
from pytest_httpx import HTTPXMock

from mentor import db
from mentor.config import Settings
from mentor.ingest import awards
from mentor.ingest.awards import AwardsError, AwardsResult, ingest_awards
from mentor.ingest.bulk import ingest_bulk

Seed = Callable[[dict | None], None]
SeedAwards = Callable[[list[dict] | None], AwardsResult]


def write(tmp_path: Path, rows: list[dict], name: str = "awards.csv") -> Path:
    path = tmp_path / name
    path.write_bytes(make_awards_zip(rows) if name.endswith(".zip") else make_awards_csv(rows))
    return path


def contract(conn: sqlite3.Connection, piid: str) -> sqlite3.Row:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM v_contracts WHERE piid = ?", (piid,)).fetchone()
    conn.row_factory = None
    return row


def unresolved(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return conn.execute(
        "SELECT alias, source_id FROM entity_aliases WHERE entity_id IS NULL ORDER BY alias"
    ).fetchall()


def test_row_mapping_and_resolution(
    conn: sqlite3.Connection, settings: Settings, seed: Seed, tmp_path: Path
) -> None:
    seed()
    result = ingest_awards(conn, settings, write(tmp_path, [{}]))
    assert result == AwardsResult(
        run_id=result.run_id, rows_read=1, rows_matched=1, contracts_new=1, contracts_updated=0,
        contractors_new=1, offices_unresolved=0, vendors_unresolved=0, resumed_from=0,
    )  # fmt: skip
    row = contract(conn, "75R60225F00001")
    assert row["awarding_office"] == "HRSA HEADQUARTERS"
    assert row["awarding_office_code"] == "75R602"
    (path_code,) = conn.execute(
        "SELECT agency_path_code FROM entities WHERE entity_id = ?", (row["awarding_entity_id"],)
    ).fetchone()
    assert path_code == "075.7526.75R602"
    assert (row["vendor"], row["vendor_uei"], row["cage"]) == (
        "LEIDOS, INC.",
        "UE9QJD4KK1L6",
        "5UTE1",
    )
    assert (row["value_usd"], row["potential_value_usd"]) == (125000.5, 250000.0)
    assert (row["award_date"], row["last_action_date"], row["pop_end"]) == (
        "2025-03-01", "2025-06-15", "2026-02-28",
    )  # fmt: skip
    assert (row["set_aside_code"], row["extent_competed_code"], row["psc_code"]) == (
        "SBA",
        "A",
        "D399",
    )
    assert row["url"] == "https://www.usaspending.gov/award/CONT_AWD_EXAMPLE/"
    vendor = conn.execute(
        "SELECT kind, name, uei, cage FROM entities WHERE entity_id = ?", (row["vendor_entity_id"],)
    ).fetchone()
    assert vendor == ("contractor", "LEIDOS, INC.", "UE9QJD4KK1L6", "5UTE1")
    aliases = conn.execute(
        "SELECT alias, method FROM entity_aliases WHERE source_id = 'usaspending_awards'"
        " ORDER BY alias"
    ).fetchall()
    assert aliases == [("LEIDOS, INC.", "exact_key")]  # the office name was already an alias
    assert conn.execute(
        "SELECT count(*) FROM entity_aliases WHERE alias = 'HRSA HEADQUARTERS' AND entity_id = ?",
        (row["awarding_entity_id"],),
    ).fetchone() == (1,)
    (status, records) = conn.execute(
        "SELECT status, records_returned FROM ingestion_runs WHERE run_id = ?", (result.run_id,)
    ).fetchone()
    assert (status, records) == ("succeeded", 1)


def test_raw_json_drops_vendor_contacts_and_officers(
    conn: sqlite3.Connection, settings: Settings, tmp_path: Path
) -> None:
    ingest_awards(conn, settings, write(tmp_path, [{}]))
    (raw,) = conn.execute("SELECT raw_json FROM contracts").fetchone()
    stored = json.loads(raw)
    assert stored["awarding_office_code"] == "75R602"
    assert not {"recipient_phone_number", "recipient_fax_number"} & stored.keys()
    assert not any(key.startswith("highly_compensated_officer") for key in stored)
    assert "Officer Placeholder" not in raw and "5555550100" not in raw


def test_unknown_office_stays_unresolved_and_the_contract_is_kept(
    conn: sqlite3.Connection, settings: Settings, seed: Seed, tmp_path: Path
) -> None:
    seed()
    rows = [{"awarding_office_code": "ZZ9999", "awarding_office_name": "NOWHERE OFFICE"}]
    result = ingest_awards(conn, settings, write(tmp_path, rows))
    assert result.offices_unresolved == 1 and result.contracts_new == 1
    assert contract(conn, "75R60225F00001")["awarding_entity_id"] is None
    assert contract(conn, "75R60225F00001")["awarding_office"] == "NOWHERE OFFICE"
    assert unresolved(conn) == [("NOWHERE OFFICE", "usaspending_awards")]
    ingest_awards(conn, settings, write(tmp_path, rows, "again.csv"))
    assert unresolved(conn) == [("NOWHERE OFFICE", "usaspending_awards")]
    assert conn.execute("SELECT count(*) FROM entities WHERE kind = 'office'").fetchone() == (
        conn.execute("SELECT count(*) FROM entities WHERE kind = 'office'").fetchone()
    )


def test_deep_and_shallow_twins_resolve_to_the_deep_office(
    conn: sqlite3.Connection, settings: Settings, seed: Seed, tmp_path: Path
) -> None:
    seed()  # the API fixture carries 021.2100.AMC.ACC.MICC.FDO EUSTIS.W91QF0
    extract = tmp_path / "bulk.csv"
    extract.write_bytes(
        make_extract([{"CGAC": "021", "FPDS Code": "2100", "AAC Code": "W91QF0",
                       "Department/Ind.Agency": "DEPT OF DEFENSE", "Sub-Tier": "DEPT OF THE ARMY",
                       "Office": "FDO EUSTIS OFFICE"}])
    )  # fmt: skip
    ingest_bulk(conn, settings, extract)
    twins = conn.execute(
        "SELECT agency_path_code FROM entities WHERE agency_path_code LIKE '%.W91QF0'"
    ).fetchall()
    assert len(twins) == 2
    ingest_awards(conn, settings, write(tmp_path, [{"awarding_office_code": "W91QF0"}]))
    (path_code,) = conn.execute(
        "SELECT e.agency_path_code FROM contracts c JOIN entities e"
        " ON e.entity_id = c.awarding_entity_id"
    ).fetchone()
    assert path_code == "021.2100.AMC.ACC.MICC.FDO EUSTIS.W91QF0"


def test_same_code_under_two_agencies_is_ambiguous(
    conn: sqlite3.Connection, settings: Settings, seed: Seed, tmp_path: Path
) -> None:
    seed()
    conn.execute(
        "INSERT INTO entities (kind, name, agency_path_code, source_id, first_seen_at,"
        " last_seen_at) VALUES ('office', 'OTHER 75R602', '099.9900.75R602', 'sam_bulk_csv', ?, ?)",
        (db.utcnow(), db.utcnow()),
    )
    result = ingest_awards(conn, settings, write(tmp_path, [{}]))
    assert result.offices_unresolved == 1
    assert contract(conn, "75R60225F00001")["awarding_entity_id"] is None
    assert unresolved(conn) == [("HRSA HEADQUARTERS", "usaspending_awards")]


def test_contractor_resight_bumps_last_seen_and_collects_names(
    conn: sqlite3.Connection, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "utcnow", lambda: "2026-09-01T00:00:00Z")
    key = {"contract_award_unique_key": "CONT_AWD_SAME_7526"}
    ingest_awards(conn, settings, write(tmp_path, [key]))
    monkeypatch.setattr(db, "utcnow", lambda: "2026-09-08T00:00:00Z")
    result = ingest_awards(
        conn, settings, write(tmp_path, [{**key, "recipient_name": "LEIDOS INC"}], "later.csv")
    )
    assert (result.contracts_new, result.contracts_updated, result.contractors_new) == (0, 1, 0)
    assert conn.execute("SELECT count(*) FROM entities WHERE kind = 'contractor'").fetchone() == (
        1,
    )
    (name, first, last) = conn.execute(
        "SELECT name, first_seen_at, last_seen_at FROM entities WHERE uei = 'UE9QJD4KK1L6'"
    ).fetchone()
    assert (name, first, last) == ("LEIDOS, INC.", "2026-09-01T00:00:00Z", "2026-09-08T00:00:00Z")
    aliases = conn.execute(
        "SELECT alias FROM entity_aliases WHERE entity_id = (SELECT entity_id FROM entities"
        " WHERE uei = 'UE9QJD4KK1L6') ORDER BY alias"
    ).fetchall()
    assert aliases == [("LEIDOS INC",), ("LEIDOS, INC.",)]
    assert contract(conn, "75R60225F00001")["last_seen_at"] == "2026-09-08T00:00:00Z"


def test_cage_is_never_taken_from_another_entity(
    conn: sqlite3.Connection, settings: Settings, tmp_path: Path
) -> None:
    rows = [
        {},
        {"recipient_uei": "KTMAJCY6JXM3", "recipient_name": "LEIDOS, INC.", "cage_code": "5UTE1",
         "award_id_piid": "OTHER1"},
    ]  # fmt: skip
    ingest_awards(conn, settings, write(tmp_path, rows))
    cages = dict(conn.execute("SELECT uei, cage FROM entities WHERE kind = 'contractor'"))
    assert cages == {"UE9QJD4KK1L6": "5UTE1", "KTMAJCY6JXM3": None}
    assert contract(conn, "OTHER1")["cage"] == "5UTE1"  # the source's value stays on the award


def test_missing_uei_stays_unresolved(
    conn: sqlite3.Connection, settings: Settings, tmp_path: Path
) -> None:
    rows = [{"recipient_uei": "", "cage_code": "", "recipient_name": "SOME VENDOR"}]
    result = ingest_awards(conn, settings, write(tmp_path, rows))
    assert (result.vendors_unresolved, result.contractors_new, result.contracts_new) == (1, 0, 1)
    assert contract(conn, "75R60225F00001")["vendor"] == "SOME VENDOR"
    assert contract(conn, "75R60225F00001")["vendor_entity_id"] is None
    assert ("SOME VENDOR", "usaspending_awards") in unresolved(conn)


def test_naics_filter_limit_and_resume(
    conn: sqlite3.Connection, settings: Settings, tmp_path: Path
) -> None:
    rows = [
        {"naics_code": "541511", "award_id_piid": "SKIPPED"},
        {"award_id_piid": "FIRST"},
        {"award_id_piid": "SECOND"},
    ]
    path = write(tmp_path, rows)
    first = ingest_awards(conn, settings, path, limit=1)
    assert (first.rows_read, first.rows_matched, first.resumed_from) == (2, 1, 0)
    second = ingest_awards(conn, settings, path)
    assert (second.rows_read, second.rows_matched, second.resumed_from) == (1, 1, 2)
    piids = [p for (p,) in conn.execute("SELECT piid FROM contracts ORDER BY piid")]
    assert piids == ["FIRST", "SECOND"]
    (cursor,) = conn.execute(
        "SELECT cursor FROM ingestion_runs WHERE run_id = ?", (second.run_id,)
    ).fetchone()
    assert cursor.endswith(":541512:3")


def test_zip_member_is_read_directly(
    conn: sqlite3.Connection, settings: Settings, tmp_path: Path
) -> None:
    result = ingest_awards(conn, settings, write(tmp_path, [{}, {"award_id_piid": "B"}], "a.zip"))
    assert result.contracts_new == 2


def test_missing_column_fails_the_run(
    conn: sqlite3.Connection, settings: Settings, tmp_path: Path
) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("contract_award_unique_key,award_id_piid\r\nX,Y\r\n")
    with pytest.raises(AwardsError, match="missing columns"):
        ingest_awards(conn, settings, path)
    assert conn.execute("SELECT status FROM ingestion_runs").fetchall() == [("failed",)]
    assert conn.execute("SELECT count(*) FROM contracts").fetchone() == (0,)


def test_fetch_awards_requests_waits_and_downloads(
    settings: Settings, httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    fixtures = Path(__file__).with_name("fixtures")
    ticket = json.loads((fixtures / "usaspending_download_awards.json").read_text())
    status = json.loads((fixtures / "usaspending_download_status.json").read_text())
    httpx_mock.add_response(url=re.compile(r".*/download/awards/"), json=ticket)
    httpx_mock.add_response(url=re.compile(r".*/download/status.*"), json=status)
    httpx_mock.add_response(url=ticket["file_url"], content=make_awards_zip([{}]))
    path = awards.fetch_awards(
        settings, since=date(2024, 10, 1), until=date(2025, 9, 30), sleep=lambda _: None
    )
    assert path == tmp_path / "extracts" / "usaspending" / ticket["file_name"]
    body = json.loads(httpx_mock.get_requests()[0].content)
    assert body["filters"] == {
        "time_period": [
            {"start_date": "2024-10-01", "end_date": "2025-09-30", "date_type": "action_date"}
        ],
        "award_type_codes": ["A", "B", "C", "D"],
        "naics_codes": {"require": ["541512"]},
    }


def test_cancel_between_batches_keeps_committed_awards(
    conn: sqlite3.Connection, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mentor.progress import JobCancelled

    monkeypatch.setattr(awards, "BATCH", 1)
    path = write(tmp_path, [{"award_id_piid": "A"}, {"award_id_piid": "B"}])
    lines: list[str] = []
    with pytest.raises(JobCancelled):
        ingest_awards(conn, settings, path, report=lines.append, cancelled=lambda: len(lines) >= 1)
    assert lines == ["1 awards in slice, 1 rows read"]
    assert [p for (p,) in conn.execute("SELECT piid FROM contracts")] == ["A"]
    assert ingest_awards(conn, settings, path).resumed_from == 1


def test_a_prefix_slice_takes_the_whole_industry_group(
    conn: sqlite3.Connection, settings: Settings, seed: Seed, tmp_path: Path
) -> None:
    """Before this, a four-digit slice matched nothing and the run still reported success."""
    seed()
    for prefix in ("5415", "54"):
        wide = settings.model_copy(update={"naics": [prefix]})
        assert ingest_awards(conn, wide, write(tmp_path, [{}])).rows_matched == 1
    miss = settings.model_copy(update={"naics": ["5416"]})
    assert ingest_awards(conn, miss, write(tmp_path, [{}])).rows_matched == 0


def test_the_run_records_its_window_and_what_each_slice_element_took(
    conn: sqlite3.Connection, settings: Settings, seed: Seed, tmp_path: Path
) -> None:
    """Without this a zero-row run is indistinguishable from a quiet market."""
    seed()
    wide = settings.model_copy(update={"naics": ["5415", "5416"]})
    lines: list[str] = []
    result = ingest_awards(
        conn,
        wide,
        write(tmp_path, [{}]),
        since=date(2023, 9, 7),
        until=date(2026, 9, 7),
        report=lines.append,
    )
    row = conn.execute(
        "SELECT posted_from, posted_to, filter_json FROM ingestion_runs WHERE run_id = ?",
        (result.run_id,),
    ).fetchone()
    assert row[0] == "2023-09-07" and row[1] == "2026-09-07"
    assert json.loads(row[2]) == {"naics": {"5415": 1, "5416": 0}}
    assert "NAICS 5416 matched nothing in this file" in lines


def test_an_ingest_from_a_supplied_file_records_no_window(
    conn: sqlite3.Connection, settings: Settings, seed: Seed, tmp_path: Path
) -> None:
    seed()
    result = ingest_awards(conn, settings, write(tmp_path, [{}]))
    assert conn.execute(
        "SELECT posted_from, posted_to FROM ingestion_runs WHERE run_id = ?", (result.run_id,)
    ).fetchone() == (None, None)


def test_members_are_ordered_by_number_not_lexicographically() -> None:
    names = [f"Contracts_PrimeAwardSummaries_x_{n}.csv" for n in (1, 2, 10, 11)]
    assert sorted(reversed(names), key=awards._member_order) == names


def test_every_prime_member_of_the_zip_is_read(
    conn: sqlite3.Connection, settings: Settings, tmp_path: Path
) -> None:
    """USAspending splits a large download across numbered members, each with its own header;
    reading only the first stops silently at its end."""
    rows = [{"award_id_piid": f"P{n}"} for n in range(6)]
    path = tmp_path / "split.zip"
    path.write_bytes(make_awards_zip(rows, members=3))
    result = ingest_awards(conn, settings, path)
    assert (result.rows_read, result.contracts_new) == (6, 6)
    assert [p for (p,) in conn.execute("SELECT piid FROM contracts ORDER BY piid")] == [
        f"P{n}" for n in range(6)
    ]


def test_a_resume_lands_inside_a_later_member(
    conn: sqlite3.Connection, settings: Settings, tmp_path: Path
) -> None:
    """The cursor is a row offset across the whole file, so it has to survive the seam."""
    rows = [{"award_id_piid": f"P{n}"} for n in range(6)]
    path = tmp_path / "split.zip"
    path.write_bytes(make_awards_zip(rows, members=3))
    first = ingest_awards(conn, settings, path, limit=4)
    assert (first.rows_read, first.resumed_from) == (4, 0)
    second = ingest_awards(conn, settings, path)
    assert (second.rows_read, second.resumed_from) == (2, 4)
    assert [p for (p,) in conn.execute("SELECT piid FROM contracts ORDER BY piid")] == [
        f"P{n}" for n in range(6)
    ]
