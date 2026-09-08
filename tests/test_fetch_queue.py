import copy
import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from mentor import workspace
from mentor.config import Settings
from mentor.fetch.queue import _html_to_text, fetch_pending, queue_status
from mentor.ingest.notices import _deadline_utc
from mentor.query import Filters

FIXTURE = json.loads((Path(__file__).with_name("fixtures") / "sam_search_v2.json").read_text())
DESC_BODY = json.loads(
    (Path(__file__).with_name("fixtures") / "sam_noticedesc_v1.json").read_text()
)
DESC_TEXT = "Example notice description. The agency's requirement is described here."
NOTICEDESC = re.compile(r".*noticedesc.*")
FILES = re.compile(r".*resources/files/.*")
PDF = b"%PDF-1.4 doc one"
PDF_HEADERS = {"Content-Disposition": "attachment; filename=Doc+One.pdf"}

Seed = Callable[[dict | None], None]


def priority_order(records: list[dict]) -> list[dict]:
    """The fixture's records in the queue's order: soonest deadline, undated last, newest first."""
    return sorted(
        records,
        key=lambda r: (
            r["responseDeadLine"] is None,
            _deadline_utc(r["responseDeadLine"]) or "",
            r["postedDate"],
        ),
    )


def test_descriptions_fetched_in_deadline_order_as_plain_text(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings, seed: Seed
) -> None:
    seed()
    httpx_mock.add_response(url=NOTICEDESC, json=DESC_BODY, is_reusable=True)

    result = fetch_pending(conn, settings, max_attachments=0)

    assert (result.descriptions_fetched, result.descriptions_failed) == (5, 0)
    assert result.requests_spent == 5 and result.budget_exhausted is False
    expected = [r["noticeId"] for r in priority_order(FIXTURE["opportunitiesData"])]
    assert expected[-1] == FIXTURE["opportunitiesData"][0]["noticeId"]  # the undated one
    requested = conn.execute(
        "SELECT notice_id FROM api_requests WHERE notice_id IS NOT NULL ORDER BY request_id"
    ).fetchall()
    assert [nid for (nid,) in requested] == expected
    rows = conn.execute("SELECT DISTINCT description, description_status FROM notices").fetchall()
    assert rows == [(DESC_TEXT, "fetched")]
    (hits,) = conn.execute(
        "SELECT count(*) FROM notices_fts WHERE notices_fts MATCH 'requirement'"
    ).fetchone()
    assert hits == 5


def test_equal_deadlines_order_by_newest_posting(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings, seed: Seed
) -> None:
    payload = copy.deepcopy(FIXTURE)
    older, newer = payload["opportunitiesData"][1], payload["opportunitiesData"][2]
    older["responseDeadLine"] = newer["responseDeadLine"] = "2026-09-01T12:00:00Z"
    older["postedDate"], newer["postedDate"] = "2026-09-01", "2026-09-05"
    seed(payload)
    httpx_mock.add_response(url=NOTICEDESC, json=DESC_BODY, is_reusable=True)

    fetch_pending(conn, settings, budget=2, max_attachments=0)

    requested = conn.execute(
        "SELECT notice_id FROM api_requests WHERE notice_id IS NOT NULL ORDER BY request_id"
    ).fetchall()
    assert requested == [(newer["noticeId"],), (older["noticeId"],)]


def test_description_failure_marks_failed_and_continues(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings, seed: Seed
) -> None:
    seed()
    httpx_mock.add_response(url=NOTICEDESC, status_code=500)
    httpx_mock.add_response(url=NOTICEDESC, json=DESC_BODY, is_reusable=True)

    result = fetch_pending(conn, settings, max_attachments=0)

    assert (result.descriptions_fetched, result.descriptions_failed) == (4, 1)
    failed = conn.execute(
        "SELECT description FROM notices WHERE description_status = 'failed'"
    ).fetchall()
    assert failed == [(None,)]


def test_quota_exhaustion_stops_descriptions_not_attachments(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings, seed: Seed
) -> None:
    seed()  # spends 1
    httpx_mock.add_response(url=NOTICEDESC, json=DESC_BODY, is_reusable=True)
    httpx_mock.add_response(url=FILES, content=PDF, headers=PDF_HEADERS, is_reusable=True)
    tight = settings.model_copy(update={"sam_daily_budget": 3})

    result = fetch_pending(conn, tight, max_attachments=2)

    assert result.descriptions_fetched == 2 and result.budget_exhausted is True
    assert result.attachments_fetched == 2
    assert result.requests_spent == 2
    (pending,) = conn.execute(
        "SELECT count(*) FROM notices WHERE description_status = 'pending'"
    ).fetchone()
    assert pending == 3
    assert conn.execute(
        "SELECT status FROM ingestion_runs WHERE run_id = ?", (result.run_id,)
    ).fetchone() == ("succeeded",)


def test_budget_argument_caps_descriptions_without_flag(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings, seed: Seed
) -> None:
    seed()
    httpx_mock.add_response(url=NOTICEDESC, json=DESC_BODY, is_reusable=True)

    result = fetch_pending(conn, settings, budget=2, max_attachments=0)

    assert result.descriptions_fetched == 2 and result.budget_exhausted is False


def test_attachment_download_records_row_and_file(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings, seed: Seed
) -> None:
    seed()
    httpx_mock.add_response(url=FILES, content=PDF, headers=PDF_HEADERS)
    first = next(r for r in priority_order(FIXTURE["opportunitiesData"]) if r["resourceLinks"])

    result = fetch_pending(conn, settings, budget=0, max_attachments=1)

    assert result.attachments_fetched == 1
    assert str(httpx_mock.get_request(url=FILES).url) == first["resourceLinks"][0]
    row = conn.execute(
        "SELECT notice_id, filename, path, content_hash, fetched_at, ingestion_run_id"
        " FROM attachments WHERE fetch_status = 'fetched'"
    ).fetchone()
    sha = hashlib.sha256(PDF).hexdigest()
    assert row[:4] == (
        first["noticeId"],
        "Doc One.pdf",
        f"attachments/{first['noticeId']}/{sha[:16]}-Doc One.pdf",
        sha,
    )
    assert row[4] is not None and row[5] == result.run_id
    assert (settings.data_dir / row[2]).read_bytes() == PDF


def test_attachment_failure_marks_failed_and_continues(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings, seed: Seed
) -> None:
    seed()
    httpx_mock.add_response(url=FILES, status_code=404)
    httpx_mock.add_response(url=FILES, content=PDF, headers=PDF_HEADERS, is_reusable=True)

    result = fetch_pending(conn, settings, budget=0, max_attachments=2)

    assert (result.attachments_fetched, result.attachments_failed) == (1, 1)
    row = conn.execute(
        "SELECT path, fetched_at, ingestion_run_id FROM attachments WHERE fetch_status = 'failed'"
    ).fetchone()
    assert row[0] is None and row[1] is not None and row[2] == result.run_id


def test_fetch_delay_between_downloads_only(
    httpx_mock: HTTPXMock,
    conn: sqlite3.Connection,
    settings: Settings,
    seed: Seed,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed()
    httpx_mock.add_response(url=FILES, content=PDF, headers=PDF_HEADERS, is_reusable=True)
    calls: list[float] = []
    monkeypatch.setattr(time, "sleep", calls.append)

    fetch_pending(
        conn, settings.model_copy(update={"fetch_delay": 0.5}), budget=0, max_attachments=3
    )

    assert calls == [0.5, 0.5]


def test_oversize_attachment_is_skipped(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings, seed: Seed
) -> None:
    seed()
    httpx_mock.add_response(url=FILES, content=PDF, headers=PDF_HEADERS)

    result = fetch_pending(
        conn, settings.model_copy(update={"max_attachment_bytes": 5}), budget=0, max_attachments=1
    )

    assert result.attachments_skipped == 1
    assert conn.execute(
        "SELECT count(*) FROM attachments WHERE fetch_status = 'skipped'"
    ).fetchone() == (1,)
    assert not [p for p in (settings.data_dir / "attachments").rglob("*") if p.is_file()]


def test_second_run_fetches_nothing(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings, seed: Seed
) -> None:
    seed()
    httpx_mock.add_response(url=NOTICEDESC, json=DESC_BODY, is_reusable=True)
    httpx_mock.add_response(url=FILES, content=PDF, headers=PDF_HEADERS, is_reusable=True)
    fetch_pending(conn, settings)

    result = fetch_pending(conn, settings)

    assert (result.descriptions_fetched, result.attachments_fetched, result.requests_spent) == (
        0,
        0,
        0,
    )
    assert queue_status(conn).descriptions_pending == 0


def test_queue_status_lists_next_descriptions(conn: sqlite3.Connection, seed: Seed) -> None:
    seed()
    status = queue_status(conn, limit=2)
    assert (status.descriptions_pending, status.attachments_pending) == (5, 20)
    expected = [r["noticeId"] for r in priority_order(FIXTURE["opportunitiesData"])[:2]]
    assert [item.notice_id for item in status.next_descriptions] == expected


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("<p>Agency&#39;s need &amp; scope.</p>\n", "Agency's need & scope."),
        ("<p>One</p><p>Two</p>", "One\nTwo"),
        ("Line<br/>break", "Line\nbreak"),
        ("<p>Some <b>bold</b> and <a href='x'>linked</a> text</p>", "Some bold and linked text"),
        ("<p>  spaced   out \t words </p>", "spaced out words"),
        ("", ""),
    ],
)
def test_html_to_text(html: str, expected: str) -> None:
    assert _html_to_text(html) == expected


def test_saved_search_leads_the_queue(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings, seed: Seed
) -> None:
    seed()
    (dod,) = conn.execute(
        "SELECT notice_id FROM notices WHERE full_parent_path_code = '097'"
    ).fetchone()
    assert dod != priority_order(FIXTURE["opportunitiesData"])[0]["noticeId"]
    workspace.save_search(conn, "dod", filters=Filters(agency_prefixes=("097",)))
    httpx_mock.add_response(url=NOTICEDESC, json=DESC_BODY)
    httpx_mock.add_response(url=FILES, content=PDF, headers=PDF_HEADERS)

    assert queue_status(conn).next_descriptions[0].notice_id == dod
    fetch_pending(conn, settings, budget=1, max_attachments=1)

    (requested,) = conn.execute(
        "SELECT notice_id FROM api_requests WHERE notice_id IS NOT NULL"
    ).fetchone()
    assert requested == dod
    (fetched,) = conn.execute(
        "SELECT notice_id FROM attachments WHERE fetch_status = 'fetched'"
    ).fetchone()
    assert fetched == dod


def test_fetch_reports_and_cancels_between_items(
    conn: sqlite3.Connection, settings: Settings, seed: Seed, httpx_mock: HTTPXMock
) -> None:
    from mentor.progress import JobCancelled

    seed()
    httpx_mock.add_response(url=NOTICEDESC, json=DESC_BODY, is_reusable=True)
    lines: list[str] = []
    with pytest.raises(JobCancelled):
        fetch_pending(conn, settings, report=lines.append, cancelled=lambda: len(lines) >= 1)
    assert len(lines) == 1 and lines[0].startswith("description: ")
    assert conn.execute(
        "SELECT count(*) FROM notices WHERE description_status = 'fetched'"
    ).fetchone() == (1,)
    (status, error) = conn.execute(
        "SELECT status, error FROM ingestion_runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    assert (status, error) == ("failed", "cancelled")
