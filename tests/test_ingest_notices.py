import copy
import json
import re
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from mentor import db
from mentor.config import Settings
from mentor.ingest.notices import _deadline_utc, ingest_notices
from mentor.quota import BudgetExceeded

FIXTURE = json.loads((Path(__file__).with_name("fixtures") / "sam_search_v2.json").read_text())
SEARCH = re.compile(r".*/opportunities/v2/search.*")
WINDOW = {"posted_from": date(2026, 9, 5), "posted_to": date(2026, 9, 6)}


def fixture_copy() -> dict:
    return copy.deepcopy(FIXTURE)


def count(conn: sqlite3.Connection, table: str) -> int:
    (n,) = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
    return n


def test_first_run_populates_graph(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings
) -> None:
    httpx_mock.add_response(url=SEARCH, json=FIXTURE)

    result = ingest_notices(conn, settings, **WINDOW)

    assert (result.notices_seen, result.notices_new, result.versions_added) == (5, 5, 5)
    assert result.attachments_added == 20
    assert result.requests_spent == 1
    assert count(conn, "notices") == 5
    assert count(conn, "entities") == 19
    assert count(conn, "entity_aliases") == 19

    chain = conn.execute(
        "SELECT entity_id, kind, name, parent_entity_id FROM entities"
        " WHERE agency_path_code IN ('075', '075.7526', '075.7526.75R602')"
        " ORDER BY length(agency_path_code)"
    ).fetchall()
    assert [row[1] for row in chain] == ["agency", "office", "office"]
    assert chain[0][2] == "HEALTH AND HUMAN SERVICES, DEPARTMENT OF"
    assert chain[0][3] is None
    assert chain[1][3] == chain[0][0] and chain[2][3] == chain[1][0]
    (agency_entity_id,) = conn.execute(
        "SELECT agency_entity_id FROM notices WHERE notice_id = ?",
        (FIXTURE["opportunitiesData"][0]["noticeId"],),
    ).fetchone()
    assert agency_entity_id == chain[2][0]

    run = conn.execute("SELECT status, records_returned FROM ingestion_runs").fetchone()
    assert run == ("succeeded", 5)


def test_second_identical_run_changes_nothing_but_last_seen(
    httpx_mock: HTTPXMock,
    conn: sqlite3.Connection,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    httpx_mock.add_response(url=SEARCH, json=FIXTURE, is_reusable=True)
    ingest_notices(conn, settings, **WINDOW)
    (first_seen,) = conn.execute("SELECT first_seen_at FROM notices LIMIT 1").fetchone()
    monkeypatch.setattr(db, "utcnow", lambda: "2026-09-07T00:00:00Z")

    result = ingest_notices(conn, settings, **WINDOW)

    assert (result.notices_new, result.versions_added, result.attachments_added) == (0, 0, 0)
    assert count(conn, "entities") == 19
    assert count(conn, "notice_versions") == 5
    rows = conn.execute("SELECT DISTINCT first_seen_at, last_seen_at FROM notices").fetchall()
    assert rows == [(first_seen, "2026-09-07T00:00:00Z")]


def test_changed_deadline_adds_one_version(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings
) -> None:
    changed = fixture_copy()
    changed["opportunitiesData"][1]["responseDeadLine"] = "2026-09-12T08:00:00-04:00"
    httpx_mock.add_response(url=SEARCH, json=FIXTURE)
    httpx_mock.add_response(url=SEARCH, json=changed)
    ingest_notices(conn, settings, **WINDOW)

    result = ingest_notices(conn, settings, **WINDOW)

    assert result.versions_added == 1
    assert count(conn, "notice_versions") == 6
    (deadline,) = conn.execute(
        "SELECT response_deadline FROM notices WHERE notice_id = ?",
        (changed["opportunitiesData"][1]["noticeId"],),
    ).fetchone()
    assert deadline == "2026-09-12T12:00:00Z"


def test_description_queue_state(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings
) -> None:
    payload = fixture_copy()
    payload["opportunitiesData"][4]["description"] = None
    httpx_mock.add_response(url=SEARCH, json=payload)

    ingest_notices(conn, settings, **WINDOW)

    rows = conn.execute(
        "SELECT description_status, description_url LIKE 'https://api.sam.gov/%' FROM notices"
    ).fetchall()
    assert rows.count(("pending", 1)) == 4
    assert rows.count(("none", None)) == 1


def test_misaligned_path_name_falls_back_to_prefixes(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings
) -> None:
    payload = fixture_copy()
    record = payload["opportunitiesData"][2]
    assert record["fullParentPathCode"] == "017.1700.SSP.N64710"
    record["fullParentPathName"] = "DEPT OF DEFENSE.U.S. NAVY.SSP.X"
    httpx_mock.add_response(url=SEARCH, json=payload)

    ingest_notices(conn, settings, **WINDOW)

    names = conn.execute(
        "SELECT name FROM entities WHERE agency_path_code LIKE '017%' ORDER BY agency_path_code"
    ).fetchall()
    assert names == [("017",), ("017.1700",), ("017.1700.SSP",), ("017.1700.SSP.N64710",)]
    aliases = conn.execute(
        "SELECT alias FROM entity_aliases JOIN entities USING (entity_id)"
        " WHERE agency_path_code LIKE '017%'"
    ).fetchall()
    assert aliases == [("DEPT OF DEFENSE.U.S. NAVY.SSP.X",)]


def test_null_path_code_creates_no_entity(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings
) -> None:
    payload = fixture_copy()
    payload["opportunitiesData"] = [payload["opportunitiesData"][3]]
    payload["opportunitiesData"][0]["fullParentPathCode"] = None
    payload["opportunitiesData"][0]["fullParentPathName"] = None
    payload["totalRecords"] = 1
    httpx_mock.add_response(url=SEARCH, json=payload)

    ingest_notices(conn, settings, **WINDOW)

    assert conn.execute("SELECT agency_entity_id FROM notices").fetchone() == (None,)
    assert count(conn, "entities") == 0


def test_budget_stop_keeps_committed_pages(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings
) -> None:
    httpx_mock.add_response(url=SEARCH, json=FIXTURE)
    tight = settings.model_copy(update={"sam_daily_budget": 1, "naics": ["541512", "541511"]})

    with pytest.raises(BudgetExceeded):
        ingest_notices(conn, tight, **WINDOW)

    assert count(conn, "notices") == 5
    run = conn.execute("SELECT status, error, requests_spent FROM ingestion_runs").fetchone()
    assert run[0] == "failed" and "budget" in run[1] and run[2] == 1


def test_empty_naics_is_refused(conn: sqlite3.Connection, settings: Settings) -> None:
    with pytest.raises(ValueError, match="MENTOR_NAICS"):
        ingest_notices(conn, settings.model_copy(update={"naics": []}), **WINDOW)
    assert count(conn, "ingestion_runs") == 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-14T08:00:00-04:00", "2026-09-14T12:00:00Z"),
        ("2026-09-14T08:00:00Z", "2026-09-14T08:00:00Z"),
        ("2026-09-14T08:00:00", "2026-09-14T08:00:00Z"),
        (None, None),
    ],
)
def test_deadline_utc(value: str | None, expected: str | None) -> None:
    assert _deadline_utc(value) == expected


def test_progress_lines_and_cancellation(
    conn: sqlite3.Connection, settings: Settings, httpx_mock: HTTPXMock
) -> None:
    from mentor.progress import JobCancelled

    httpx_mock.add_response(
        url=re.compile(r".*/opportunities/v2/search.*"), json=FIXTURE, is_reusable=True
    )
    lines: list[str] = []
    ingest_notices(
        conn,
        settings,
        posted_from=date(2026, 9, 5),
        posted_to=date(2026, 9, 6),
        report=lines.append,
    )
    assert lines == ["541512: page 1, 5 notices"]

    with pytest.raises(JobCancelled):
        ingest_notices(
            conn, settings, posted_from=date(2026, 9, 5), posted_to=date(2026, 9, 6),
            cancelled=lambda: True,
        )  # fmt: skip
    (status, error) = conn.execute(
        "SELECT status, error FROM ingestion_runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    assert (status, error) == ("failed", "cancelled")
    assert len(httpx_mock.get_requests()) == 1  # nothing was spent by the cancelled run
