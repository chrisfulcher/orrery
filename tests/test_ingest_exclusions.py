"""The exclusions adapter.

Every fixture firm, UEI, CAGE and SAM Number here is invented. The real file names real
companies and, in four rows out of five, real people; none of that belongs in a test. The
person columns carry a sentinel instead of a name, so a test can prove they are blanked
without ever holding one.
"""

import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from conftest import NOW, make_exclusions, write_exclusions
from pytest_httpx import HTTPXMock

from orrery.config import Settings
from orrery.ingest import exclusions
from orrery.ingest.awards import AwardsResult
from orrery.ingest.exclusions import (
    EXTRACT_URL,
    TERMINATION_DONE,
    TERMINATION_NO_CUT,
    TERMINATION_PARTIAL,
    ExclusionsError,
    extract_name,
    fetch_extract,
    ingest_extract,
    parse_row,
)
from orrery.progress import JobCancelled

SeedAwards = Callable[[list[dict] | None], AwardsResult]

SENTINEL = "SENTINEL-PERSON-COLUMN"
"""What the six person columns carry in a fixture: never a name, so a leak is visible."""

CUT_257 = "2026-09-14T00:00:00Z"
CUT_258 = "2026-09-15T00:00:00Z"
DAY_257 = date(2026, 9, 14)
DAY_258 = date(2026, 9, 15)

SAM_ONE = "S4MEX00001"
SAM_TWO = "S4MEX00002"

VENDORS = [
    {
        "recipient_uei": "EXCL00000001",
        "recipient_name": "EXAMPLE LOGISTICS LLC",
        "cage_code": "EXCL1",
        "award_id_piid": "75R60225F00011",
    },
    {
        "recipient_uei": "EXCL00000002",
        "recipient_name": "EXAMPLE SYSTEMS INC",
        "cage_code": "EXCL2",
        "award_id_piid": "75R60225F00012",
        "solicitation_identifier": "",
    },
]

PERSON_CELLS = {
    "Prefix": SENTINEL, "First": SENTINEL, "Middle": SENTINEL,
    "Last": SENTINEL, "Suffix": SENTINEL, "NPI": SENTINEL,
}  # fmt: skip

FIRM_ONE = {"Unique Entity ID": "EXCL00000001", "CAGE": "EXCL1", "SAM Number": SAM_ONE}
FIRM_BY_CAGE = {
    "Name": "EXAMPLE SYSTEMS INC",
    "Unique Entity ID": "",
    "CAGE": "EXCL2",
    "SAM Number": SAM_TWO,
    "Excluding Agency": "GSA",
}


def ids(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        uei: entity_id
        for entity_id, uei in conn.execute(
            "SELECT entity_id, uei FROM entities WHERE kind = 'contractor'"
        )
    }


def facts_of(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute(
        "SELECT subject_id, predicate, value, source_ref, observed_at, extraction_method,"
        " source_id FROM facts WHERE predicate LIKE 'sam.exclusion.%' ORDER BY fact_id"
    ).fetchall()


def test_a_firm_row_is_parsed_without_its_person_columns(tmp_path: Path) -> None:
    row = dict(_defaults(), **PERSON_CELLS)

    record = parse_row(row)

    assert all(record.record[column] == "" for column in exclusions.PERSON_COLUMNS)
    assert SENTINEL not in str(record.record) and SENTINEL not in str(record.facts())
    assert record.sam_number == SAM_ONE and record.cage == "EXCL1"
    assert record.status == "active" and record.termination_date is None
    assert [predicate for predicate, _, _ in record.facts()] == [
        "sam.exclusion.status", "sam.exclusion.type", "sam.exclusion.program",
        "sam.exclusion.agency", "sam.exclusion.ct_code", "sam.exclusion.active_date",
        "sam.exclusion.record",
    ]  # fmt: skip
    dated = parse_row(dict(row, **{"Termination Date": "2027-01-31"}))
    assert dated.termination_date == "2027-01-31"
    # Nothing about a named person is read past the classification that says they are one.
    with pytest.raises(ExclusionsError):
        parse_row(dict(row, Classification="Individual"))


def test_only_contractors_already_in_the_store_are_read(
    conn: sqlite3.Connection, settings: Settings, seed_awards: SeedAwards, tmp_path: Path
) -> None:
    seed_awards(VENDORS)
    before = _counts(conn)
    path = write_exclusions(
        tmp_path,
        [
            FIRM_ONE,
            FIRM_BY_CAGE,
            # An individual whose UEI would have matched: the classification decides, not the key.
            dict(
                _individual(),
                **{"Unique Entity ID": "EXCL00000001", "SAM Number": "S4MEX00003"},
            ),
            {"Unique Entity ID": "EXCL00000009", "CAGE": "", "SAM Number": "S4MEX00004"},
        ],
        DAY_257,
    )

    result = ingest_extract(conn, settings, path)

    assert (result.rows_read, result.individuals_skipped, result.rows_matched) == (4, 1, 2)
    assert result.exclusions_new == 2 and result.termination_pass == TERMINATION_DONE
    entities = ids(conn)
    subjects = {(str(entities["EXCL00000001"]), SAM_ONE), (str(entities["EXCL00000002"]), SAM_TWO)}
    assert {(row[0], row[3]) for row in facts_of(conn)} == subjects
    assert {row[4] for row in facts_of(conn)} == {CUT_257}
    assert {row[5] for row in facts_of(conn)} == {"extract"}
    assert {row[6] for row in facts_of(conn)} == {"sam_exclusions"}
    assert conn.execute(
        "SELECT source_generated_at, status, records_returned FROM ingestion_runs"
        " WHERE source_id = 'sam_exclusions'"
    ).fetchone() == (CUT_257, "succeeded", 2)
    # No entity, no alias: this file never mints anything.
    assert _counts(conn) == before
    assert not any(SENTINEL in row[2] for row in facts_of(conn))


def test_a_rerun_of_the_same_file_adds_no_facts(
    conn: sqlite3.Connection, settings: Settings, seed_awards: SeedAwards, tmp_path: Path
) -> None:
    seed_awards(VENDORS)
    path = write_exclusions(tmp_path, [FIRM_ONE], DAY_257)
    first = ingest_extract(conn, settings, path)

    again = ingest_extract(conn, settings, path)

    assert first.facts_added > 0 and again.facts_added == 0
    assert again.exclusions_new == 0 and again.rows_matched == 1
    assert len(facts_of(conn)) == first.facts_added


def test_an_exclusion_absent_from_a_later_file_has_ended(
    conn: sqlite3.Connection, settings: Settings, seed_awards: SeedAwards, tmp_path: Path
) -> None:
    seed_awards(VENDORS)
    ingest_extract(conn, settings, write_exclusions(tmp_path, [FIRM_ONE, FIRM_BY_CAGE], DAY_257))

    later = ingest_extract(conn, settings, write_exclusions(tmp_path, [FIRM_BY_CAGE], DAY_258))

    assert later.terminated == 1 and later.termination_pass == TERMINATION_DONE
    gone = [row for row in facts_of(conn) if row[5] == "absence"]
    assert [(row[1], row[2], row[3], row[4]) for row in gone] == [
        ("sam.exclusion.status", "terminated", SAM_ONE, CUT_258)
    ]
    assert conn.execute(
        "SELECT status, current FROM v_exclusions WHERE sam_number = ?", (SAM_TWO,)
    ).fetchone() == ("active", 1)
    assert conn.execute(
        "SELECT status, current FROM v_exclusions WHERE sam_number = ?", (SAM_ONE,)
    ).fetchone() == ("terminated", 0)


def test_a_partial_run_terminates_nothing(
    conn: sqlite3.Connection, settings: Settings, seed_awards: SeedAwards, tmp_path: Path
) -> None:
    seed_awards(VENDORS)
    ingest_extract(conn, settings, write_exclusions(tmp_path, [FIRM_ONE, FIRM_BY_CAGE], DAY_257))
    later = write_exclusions(tmp_path, [FIRM_BY_CAGE], DAY_258)

    capped = ingest_extract(conn, settings, later, limit=1)

    assert capped.terminated == 0 and capped.termination_pass == TERMINATION_PARTIAL
    assert not [row for row in facts_of(conn) if row[5] == "absence"]


def test_a_cancelled_run_terminates_nothing_and_fails(
    conn: sqlite3.Connection,
    settings: Settings,
    seed_awards: SeedAwards,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_awards(VENDORS)
    ingest_extract(conn, settings, write_exclusions(tmp_path, [FIRM_ONE, FIRM_BY_CAGE], DAY_257))
    monkeypatch.setattr(exclusions, "BATCH", 1)

    with pytest.raises(JobCancelled):
        ingest_extract(
            conn,
            settings,
            write_exclusions(tmp_path, [FIRM_BY_CAGE], DAY_258),
            cancelled=lambda: True,
        )

    assert not [row for row in facts_of(conn) if row[5] == "absence"]
    assert conn.execute(
        "SELECT status, error FROM ingestion_runs WHERE source_id = 'sam_exclusions'"
        " ORDER BY run_id DESC LIMIT 1"
    ).fetchone() == ("failed", "cancelled")


def test_an_older_file_after_a_newer_one_terminates_nothing(
    conn: sqlite3.Connection, settings: Settings, seed_awards: SeedAwards, tmp_path: Path
) -> None:
    """Absence only means an end when the file is later than what the store already saw."""
    seed_awards(VENDORS)
    ingest_extract(conn, settings, write_exclusions(tmp_path, [FIRM_ONE, FIRM_BY_CAGE], DAY_258))

    older = ingest_extract(conn, settings, write_exclusions(tmp_path, [FIRM_BY_CAGE], DAY_257))

    assert older.terminated == 0 and older.termination_pass == TERMINATION_DONE
    assert not [row for row in facts_of(conn) if row[5] == "absence"]


def test_an_undated_file_is_observed_now_and_ends_nothing(
    conn: sqlite3.Connection, settings: Settings, seed_awards: SeedAwards, tmp_path: Path
) -> None:
    seed_awards(VENDORS)
    ingest_extract(conn, settings, write_exclusions(tmp_path, [FIRM_ONE], DAY_257))

    own = ingest_extract(conn, settings, write_exclusions(tmp_path, [FIRM_BY_CAGE], None))

    assert own.termination_pass == TERMINATION_NO_CUT and own.terminated == 0
    assert conn.execute(
        "SELECT source_generated_at FROM ingestion_runs WHERE source_id = 'sam_exclusions'"
        " ORDER BY run_id DESC LIMIT 1"
    ).fetchone() == (None,)
    # The file says nothing about when it was cut, so the run's own clock is the observation.
    assert {row[4] for row in facts_of(conn) if row[3] == SAM_TWO} == {NOW}
    assert {row[4] for row in facts_of(conn) if row[3] == SAM_ONE} == {CUT_257}


def test_two_exclusions_on_one_entity_stay_distinct(
    conn: sqlite3.Connection, settings: Settings, seed_awards: SeedAwards, tmp_path: Path
) -> None:
    seed_awards(VENDORS)
    second = dict(FIRM_ONE, **{"SAM Number": SAM_TWO, "Excluding Agency": "DOE"})

    result = ingest_extract(conn, settings, write_exclusions(tmp_path, [FIRM_ONE, second], DAY_257))

    assert result.rows_matched == 2 and result.exclusions_new == 2
    rows = conn.execute(
        "SELECT sam_number, agency, current FROM v_exclusions ORDER BY sam_number"
    ).fetchall()
    assert rows == [(SAM_ONE, "TREAS", 1), (SAM_TWO, "DOE", 1)]


def test_a_changed_termination_date_appends_one_fact(
    conn: sqlite3.Connection, settings: Settings, seed_awards: SeedAwards, tmp_path: Path
) -> None:
    seed_awards(VENDORS)
    ingest_extract(conn, settings, write_exclusions(tmp_path, [FIRM_ONE], DAY_257))
    ended = dict(FIRM_ONE, **{"Termination Date": "2027-01-31"})

    changed = ingest_extract(conn, settings, write_exclusions(tmp_path, [ended], DAY_258))

    # The typed value, and the verbatim row that also changed; nothing else is rewritten.
    assert changed.facts_added == 2 and changed.exclusions_new == 0
    dates = [row for row in facts_of(conn) if row[1] == "sam.exclusion.termination_date"]
    assert [(row[2], row[4]) for row in dates] == [("2027-01-31", CUT_258)]
    assert conn.execute(
        "SELECT termination_date, current FROM v_exclusions WHERE sam_number = ?", (SAM_ONE,)
    ).fetchone() == ("2027-01-31", 1)


def test_fetch_extract_falls_back_to_yesterday_and_then_caches(
    httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    """The daily file appears during its own UTC day, so early on, today's name is a 404."""
    today = datetime.now(UTC).date()
    yesterday = date.fromordinal(today.toordinal() - 1)
    httpx_mock.add_response(url=EXTRACT_URL.format(name=extract_name(today)), status_code=404)
    httpx_mock.add_response(
        url=EXTRACT_URL.format(name=extract_name(yesterday)),
        content=make_exclusions([FIRM_ONE]),
        headers={"Last-Modified": "Sun, 13 Sep 2026 06:02:00 GMT"},
    )

    extract = fetch_extract(tmp_path / "extracts")

    assert extract.path.name == extract_name(yesterday)
    assert extract.generated_at == "2026-09-13T06:02:00Z"
    assert not list(extract.path.parent.glob("*.part"))
    assert fetch_extract(tmp_path / "extracts") == extract
    assert len(httpx_mock.get_requests()) == 2


def test_a_missing_required_column_fails_the_run(
    conn: sqlite3.Connection, settings: Settings, tmp_path: Path
) -> None:
    path = write_exclusions(
        tmp_path,
        [FIRM_ONE],
        DAY_257,
        columns=[c for c in exclusions.COLUMNS if c != "SAM Number"],
    )

    with pytest.raises(ExclusionsError, match="SAM Number"):
        ingest_extract(conn, settings, path)

    assert conn.execute(
        "SELECT status FROM ingestion_runs WHERE source_id = 'sam_exclusions'"
    ).fetchone() == ("failed",)


def _defaults() -> dict:
    from conftest import EXCLUSION_DEFAULTS

    return {column: "" for column in exclusions.COLUMNS} | {
        k: v for k, v in EXCLUSION_DEFAULTS.items() if k in set(exclusions.COLUMNS)
    }


def _individual() -> dict:
    return dict(PERSON_CELLS, Classification="Individual", Name=SENTINEL)


def _counts(conn: sqlite3.Connection) -> tuple[int, int]:
    (entities,) = conn.execute("SELECT count(*) FROM entities").fetchone()
    (aliases,) = conn.execute("SELECT count(*) FROM entity_aliases").fetchone()
    return entities, aliases
