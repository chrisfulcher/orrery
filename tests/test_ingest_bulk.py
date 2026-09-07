import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import SEARCH_FIXTURE, make_extract
from pytest_httpx import HTTPXMock

from mentor.config import Settings
from mentor.ingest import bulk
from mentor.ingest.bulk import ACTIVE_NAME, EXTRACT_URL, BulkError, fetch_extract, ingest_bulk

Seed = Callable[[dict | None], None]
HRSA = SEARCH_FIXTURE["opportunitiesData"][0]
ARMY_DEEP = SEARCH_FIXTURE["opportunitiesData"][1]


def write(tmp_path: Path, rows: list[dict], name: str = "extract.csv") -> Path:
    path = tmp_path / name
    path.write_bytes(make_extract(rows))
    return path


def count(conn: sqlite3.Connection, sql: str, *params: object) -> int:
    (n,) = conn.execute(sql, params).fetchone()
    return n


def test_row_mapping(conn: sqlite3.Connection, settings: Settings, tmp_path: Path) -> None:
    path = write(tmp_path, [{"NoticeId": "a" * 32, "SetASideCode": "", "PopCity": "Boulder"}])

    result = ingest_bulk(conn, settings, path)

    assert (result.rows_read, result.rows_matched, result.notices_new) == (1, 1, 1)
    assert result.versions_added == 1 and result.resumed_from == 0
    row = conn.execute(
        "SELECT solicitation_number, title, notice_type, full_parent_path_name,"
        " full_parent_path_code, naics_code, psc_code, set_aside_code, posted_at,"
        " response_deadline, place_of_performance, active, source_id, description,"
        " description_status, description_url, raw_json FROM notices"
    ).fetchone()
    assert row[:10] == (
        "75R60226Q00001", "Custodial Service", "Solicitation",
        "HEALTH AND HUMAN SERVICES, DEPARTMENT OF.HEALTH RESOURCES AND SERVICES ADMINISTRATION"
        ".HRSA HEADQUARTERS",
        "075.7526.75R602", "541512", "S201", None, "2026-09-05", "2026-09-15T19:00:00Z",
    )  # fmt: skip
    assert json.loads(row[10]) == {
        "city": "Boulder",
        "state": "CO",
        "zip": "80503",
        "country": "USA",
    }
    assert row[11:16] == (1, "sam_bulk_csv", "Section L – instructions\nline two", "fetched", None)
    raw = json.loads(row[16])
    assert len(raw) == 47 and raw["Sol#"] == "75R60226Q00001" and raw["SetASideCode"] == ""


def test_empty_description_and_naics_filter(
    conn: sqlite3.Connection, settings: Settings, tmp_path: Path
) -> None:
    path = write(tmp_path, [{"Description": ""}, {"NaicsCode": "236220"}, {"NaicsCode": ""}])

    result = ingest_bulk(conn, settings, path)

    assert (result.rows_read, result.rows_matched) == (3, 1)
    assert conn.execute("SELECT description, description_status FROM notices").fetchall() == [
        (None, "none")
    ]


def test_api_row_is_confirmed_and_filled_not_overwritten(
    conn: sqlite3.Connection, settings: Settings, seed: Seed, tmp_path: Path
) -> None:
    seed()
    before = conn.execute(
        "SELECT title, full_parent_path_code, agency_entity_id, source_id, first_seen_at"
        " FROM notices WHERE notice_id = ?",
        (HRSA["noticeId"],),
    ).fetchone()
    entities = count(conn, "SELECT count(*) FROM entities")
    conn.execute(
        "UPDATE notices SET description_status = 'failed' WHERE notice_id = ?", (HRSA["noticeId"],)
    )
    path = write(tmp_path, [{"NoticeId": HRSA["noticeId"], "Title": "Renamed", "Active": "No"}])

    result = ingest_bulk(conn, settings, path)

    assert (result.notices_new, result.notices_updated, result.descriptions_filled) == (0, 1, 1)
    assert result.versions_added == 0
    after = conn.execute(
        "SELECT title, full_parent_path_code, agency_entity_id, source_id, first_seen_at,"
        " description, description_status, active FROM notices WHERE notice_id = ?",
        (HRSA["noticeId"],),
    ).fetchone()
    assert after[:5] == before
    assert after[5:] == ("Section L – instructions\nline two", "fetched", 0)
    assert count(conn, "SELECT count(*) FROM entities") == entities
    assert (
        count(conn, "SELECT count(*) FROM notice_versions WHERE notice_id = ?", HRSA["noticeId"])
        == 1
    )


def test_api_overwrites_a_bulk_row_and_keeps_its_description(
    conn: sqlite3.Connection, settings: Settings, seed: Seed, tmp_path: Path
) -> None:
    ingest_bulk(conn, settings, write(tmp_path, [{"NoticeId": HRSA["noticeId"], "Title": "Bulk"}]))

    seed()

    row = conn.execute(
        "SELECT title, source_id, description, description_status FROM notices WHERE notice_id = ?",
        (HRSA["noticeId"],),
    ).fetchone()
    assert row == (
        HRSA["title"],
        "sam_opportunities_api",
        "Section L – instructions\nline two",
        "fetched",
    )
    assert (
        count(conn, "SELECT count(*) FROM notice_versions WHERE notice_id = ?", HRSA["noticeId"])
        == 2
    )


def test_bulk_rerun_versions_only_on_change(
    conn: sqlite3.Connection, settings: Settings, tmp_path: Path
) -> None:
    same = [{"NoticeId": "b" * 32}]
    ingest_bulk(conn, settings, write(tmp_path, same, "one.csv"))
    assert ingest_bulk(conn, settings, write(tmp_path, same, "two.csv")).versions_added == 0

    changed = [{"NoticeId": "b" * 32, "ResponseDeadLine": "2026-09-20T15:00:00-04:00"}]
    result = ingest_bulk(conn, settings, write(tmp_path, changed, "three.csv"))

    assert (result.notices_updated, result.versions_added) == (1, 1)
    assert conn.execute("SELECT response_deadline FROM notices").fetchone() == (
        "2026-09-20T19:00:00Z",
    )


def test_entity_resolution_agrees_with_the_api(
    conn: sqlite3.Connection, settings: Settings, seed: Seed, tmp_path: Path
) -> None:
    seed()
    entities = count(conn, "SELECT count(*) FROM entities")
    (hrsa_leaf,) = conn.execute(
        "SELECT agency_entity_id FROM notices WHERE notice_id = ?", (HRSA["noticeId"],)
    ).fetchone()
    rows = [
        {"NoticeId": "c" * 32},  # same three codes as the seeded HRSA notice
        {"NoticeId": "d" * 32, "CGAC": "070", "Department/Ind.Agency": "HOMELAND SECURITY",
         "FPDS Code": "7008", "Sub-Tier": "U.S. COAST GUARD", "AAC Code": "70Z0AA",
         "Office": "USCG HQ"},
        {"NoticeId": "e" * 32, "CGAC": "097", "Department/Ind.Agency": "DEPT OF DEFENSE",
         "FPDS Code": "", "Sub-Tier": "", "AAC Code": "", "Office": ""},
        {"NoticeId": "f" * 32, "CGAC": "", "FPDS Code": "", "AAC Code": ""},
    ]  # fmt: skip
    ingest_bulk(conn, settings, write(tmp_path, rows))

    leaves = dict(conn.execute("SELECT notice_id, agency_entity_id FROM notices").fetchall())
    assert leaves["c" * 32] == hrsa_leaf
    assert leaves["f" * 32] is None
    names = dict(conn.execute("SELECT agency_path_code, name FROM entities").fetchall())
    assert names["070.7008"] == "U.S. COAST GUARD" and names["070.7008.70Z0AA"] == "USCG HQ"
    assert (
        names["097"] == "DEPT OF DEFENSE"
        and leaves["e" * 32]
        == conn.execute("SELECT entity_id FROM entities WHERE agency_path_code = '097'").fetchone()[
            0
        ]
    )
    assert (
        count(conn, "SELECT count(*) FROM entities") == entities + 3
    )  # 070, 070.7008, leaf; 097 existed


def test_deep_api_office_gets_a_shallow_bulk_twin(
    conn: sqlite3.Connection, settings: Settings, seed: Seed, tmp_path: Path
) -> None:
    """Known limitation: the CSV cannot reproduce intermediate API path segments."""
    seed()
    codes = ARMY_DEEP["fullParentPathCode"].split(".")
    assert len(codes) == 7
    entities = count(conn, "SELECT count(*) FROM entities")
    (api_leaf,) = conn.execute(
        "SELECT agency_entity_id FROM notices WHERE notice_id = ?", (ARMY_DEEP["noticeId"],)
    ).fetchone()
    row = {"NoticeId": ARMY_DEEP["noticeId"], "CGAC": codes[0], "FPDS Code": codes[1],
           "AAC Code": codes[-1], "Department/Ind.Agency": "DEPT OF DEFENSE",
           "Sub-Tier": "DEPT OF THE ARMY", "Office": "FDO EUSTIS OFFICE"}  # fmt: skip
    ingest_bulk(conn, settings, write(tmp_path, [row]))
    ingest_bulk(conn, settings, write(tmp_path, [{**row, "NoticeId": "9" * 32}], "b.csv"))

    assert count(conn, "SELECT count(*) FROM entities") == entities + 1
    twin = conn.execute(
        "SELECT entity_id FROM entities WHERE agency_path_code = ?",
        (f"{codes[0]}.{codes[1]}.{codes[-1]}",),
    ).fetchone()[0]
    assert twin != api_leaf
    leaves = dict(conn.execute("SELECT notice_id, agency_entity_id FROM notices").fetchall())
    assert leaves[ARMY_DEEP["noticeId"]] == api_leaf and leaves["9" * 32] == twin


def test_resume_continues_from_the_cursor(
    conn: sqlite3.Connection, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bulk, "BATCH", 2)
    path = write(tmp_path, [{"NoticeId": f"{i:032x}"} for i in range(1, 6)])

    first = ingest_bulk(conn, settings, path, limit=3)
    second = ingest_bulk(conn, settings, path)
    third = ingest_bulk(conn, settings, path)

    assert (first.rows_matched, first.resumed_from) == (3, 0)
    assert (second.rows_read, second.rows_matched, second.resumed_from) == (2, 2, 3)
    assert (third.rows_read, third.resumed_from) == (0, 5)
    assert count(conn, "SELECT count(*) FROM notices") == 5
    other_naics = settings.model_copy(update={"naics": ["541512", "541511"]})
    assert ingest_bulk(conn, other_naics, path).resumed_from == 0


def test_failure_mid_batch_keeps_committed_rows_and_cursor(
    conn: sqlite3.Connection, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bulk, "BATCH", 2)
    path = write(tmp_path, [{"NoticeId": f"{i:032x}"} for i in range(1, 6)])
    real = bulk._ingest_row
    calls = {"n": 0}

    def flaky(conn, row, now):
        calls["n"] += 1
        if calls["n"] == 4:
            raise RuntimeError("disk on fire")
        return real(conn, row, now)

    monkeypatch.setattr(bulk, "_ingest_row", flaky)
    with pytest.raises(RuntimeError):
        ingest_bulk(conn, settings, path)

    assert count(conn, "SELECT count(*) FROM notices") == 2
    assert conn.execute("SELECT status FROM ingestion_runs").fetchone() == ("failed",)
    monkeypatch.setattr(bulk, "_ingest_row", real)
    assert ingest_bulk(conn, settings, path).resumed_from == 2
    assert count(conn, "SELECT count(*) FROM notices") == 5


def test_active_pass_clears_unseen_slice_notices(
    conn: sqlite3.Connection, settings: Settings, seed: Seed, tmp_path: Path
) -> None:
    seed()
    conn.execute("UPDATE notices SET last_seen_at = '2026-01-01T00:00:00Z'")
    conn.execute(
        "INSERT INTO notices (notice_id, title, naics_code, first_seen_at, last_seen_at, source_id,"
        " raw_json) VALUES ('other', 't', '541511', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z',"
        " 'sam_opportunities_api', '{}')"
    )
    seen = [{"NoticeId": r["noticeId"]} for r in SEARCH_FIXTURE["opportunitiesData"][:2]]

    assert (
        ingest_bulk(conn, settings, write(tmp_path, seen, "a.csv"), limit=1).notices_deactivated
        == 0
    )
    assert ingest_bulk(conn, settings, write(tmp_path, seen, "b.csv")).notices_deactivated == 0
    result = ingest_bulk(conn, settings, write(tmp_path, seen, "c.csv"), mark_inactive=True)

    assert result.notices_deactivated == 3
    assert count(conn, "SELECT count(*) FROM notices WHERE active = 1") == 3  # 2 seen + other
    assert conn.execute("SELECT active FROM notices WHERE notice_id = 'other'").fetchone() == (1,)


def test_missing_column_fails_the_run(
    conn: sqlite3.Connection, settings: Settings, tmp_path: Path
) -> None:
    path = tmp_path / "bad.csv"
    path.write_bytes(make_extract([{}], columns=[c for c in bulk.COLUMNS if c != "Description"]))
    with pytest.raises(BulkError, match="Description"):
        ingest_bulk(conn, settings, path)
    assert conn.execute("SELECT status FROM ingestion_runs").fetchone() == ("failed",)


def test_fetch_extract_downloads_once_per_day(httpx_mock: HTTPXMock, tmp_path: Path) -> None:
    url = EXTRACT_URL.format(name=ACTIVE_NAME)
    httpx_mock.add_response(url=url, content=make_extract([{}]))

    path = fetch_extract(tmp_path / "extracts")

    assert path.name == "ContractOpportunitiesFullCSV.csv" and path.read_bytes().startswith(
        b"NoticeId"
    )
    assert not list(path.parent.glob("*.part"))
    assert fetch_extract(tmp_path / "extracts") == path
    assert len(httpx_mock.get_requests()) == 1


def test_fetch_extract_failure_leaves_nothing(httpx_mock: HTTPXMock, tmp_path: Path) -> None:
    httpx_mock.add_response(url=EXTRACT_URL.format(name=bulk.archive_name(2025)), status_code=404)
    with pytest.raises(BulkError, match="HTTP 404"):
        fetch_extract(tmp_path / "extracts", fiscal_year=2025)
    assert not list((tmp_path / "extracts").iterdir())
