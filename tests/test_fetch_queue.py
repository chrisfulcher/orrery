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

from orrery import workspace
from orrery.config import Settings
from orrery.fetch.queue import _html_to_text, fetch_pending, queue_status
from orrery.ingest.notices import _deadline_utc
from orrery.query import Filters
from orrery.sam.client import ManifestShapeError

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

    result = fetch_pending(conn, settings, max_attachments=0, max_manifests=0)

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

    fetch_pending(conn, settings, budget=2, max_attachments=0, max_manifests=0)

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

    result = fetch_pending(conn, settings, max_attachments=0, max_manifests=0)

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

    result = fetch_pending(conn, tight, max_attachments=2, max_manifests=0)

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

    result = fetch_pending(conn, settings, budget=2, max_attachments=0, max_manifests=0)

    assert result.descriptions_fetched == 2 and result.budget_exhausted is False


def test_attachment_download_records_row_and_file(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings, seed: Seed
) -> None:
    seed()
    httpx_mock.add_response(url=FILES, content=PDF, headers=PDF_HEADERS)
    first = next(r for r in priority_order(FIXTURE["opportunitiesData"]) if r["resourceLinks"])

    result = fetch_pending(conn, settings, budget=0, max_attachments=1, max_manifests=0)

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

    result = fetch_pending(conn, settings, budget=0, max_attachments=2, max_manifests=0)

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
        conn,
        settings.model_copy(update={"fetch_delay": 0.5}),
        budget=0,
        max_attachments=3,
        max_manifests=0,
    )

    assert calls == [0.5, 0.5]


def test_oversize_attachment_is_skipped(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings, seed: Seed
) -> None:
    seed()
    httpx_mock.add_response(url=FILES, content=PDF, headers=PDF_HEADERS)

    result = fetch_pending(
        conn,
        settings.model_copy(update={"max_attachment_bytes": 5}),
        budget=0,
        max_attachments=1,
        max_manifests=0,
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
    fetch_pending(conn, settings, max_manifests=0)

    result = fetch_pending(conn, settings, max_manifests=0)

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
    fetch_pending(conn, settings, budget=1, max_attachments=1, max_manifests=0)

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
    from orrery.progress import JobCancelled

    seed()
    httpx_mock.add_response(url=NOTICEDESC, json=DESC_BODY, is_reusable=True)
    lines: list[str] = []
    with pytest.raises(JobCancelled):
        fetch_pending(
            conn,
            settings,
            max_manifests=0,
            report=lines.append,
            cancelled=lambda: len(lines) >= 1,
        )
    assert len(lines) == 1 and lines[0].startswith("description: ")
    assert conn.execute(
        "SELECT count(*) FROM notices WHERE description_status = 'fetched'"
    ).fetchone() == (1,)
    (status, error) = conn.execute(
        "SELECT status, error FROM ingestion_runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    assert (status, error) == ("failed", "cancelled")


MANIFEST = re.compile(r".*/opportunities/[0-9a-f]+/resources")


def manifest(*entries: dict) -> dict:
    return {"_embedded": {"opportunityAttachmentList": [{"attachments": list(entries)}]}}


def entry(resource_id: str, **over: object) -> dict:
    base = {
        "resourceId": resource_id,
        "name": f"{resource_id}.pdf",
        "mimeType": ".pdf",
        "size": 1024,
        "accessStatus": "public",
        "exportControlled": "0",
        "deletedFlag": "0",
    }
    return {**base, **over}


def attachment_rows(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute(
        "SELECT filename, fetch_status, discovered_by, declared_size, mime_type"
        " FROM attachments ORDER BY attachment_id"
    ).fetchall()


def test_manifests_discover_attachments_for_notices_that_have_none(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings, seed: Seed
) -> None:
    """The whole point of #23: a bulk-backfilled notice carries no resourceLinks, so without
    this stage its attachments are never known about at all."""
    seed()
    conn.execute("DELETE FROM attachments")  # as a bulk-sourced store looks
    httpx_mock.add_response(url=MANIFEST, json=manifest(entry("r1"), entry("r2")), is_reusable=True)

    result = fetch_pending(conn, settings, budget=0, max_attachments=0)

    assert result.manifests_checked == 5 and result.manifests_failed == 0
    assert result.attachments_found == 10
    assert attachment_rows(conn)[0] == ("r1.pdf", "pending", "manifest", 1024, ".pdf")
    assert conn.execute(
        "SELECT count(*) FROM notices WHERE manifest_status = 'checked'"
    ).fetchone() == (5,)


def test_a_notice_with_no_attachments_is_recorded_as_checked(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings, seed: Seed
) -> None:
    """A 200 with no _embedded is a result. Recorded, or the notice is asked about forever."""
    seed()
    httpx_mock.add_response(url=MANIFEST, json={"_links": {}}, is_reusable=True)

    first = fetch_pending(conn, settings, budget=0, max_attachments=0)
    assert first.manifests_checked == 5 and first.attachments_found == 0

    second = fetch_pending(conn, settings, budget=0, max_attachments=0)
    assert second.manifests_checked == 0  # nothing is due again


def test_an_unknown_notice_is_terminal_and_a_failure_comes_round_again(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings, seed: Seed
) -> None:
    httpx_mock.add_response(url=MANIFEST, status_code=400, is_reusable=True)
    seed()

    fetch_pending(conn, settings, budget=0, max_attachments=0)

    assert conn.execute(
        "SELECT count(*) FROM notices WHERE manifest_status = 'unknown'"
    ).fetchone() == (5,)
    # 'unknown' is never asked about again; a 'failed' row would be, under the interval rule.
    assert fetch_pending(conn, settings, budget=0, max_attachments=0).manifests_checked == 0


def test_a_manifest_the_release_cannot_parse_fails_the_run_after_downloads(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings, seed: Seed
) -> None:
    """The endpoint has no published contract, so this is the expected way it breaks: stop on
    the first one rather than writing rows we do not understand, but let queued downloads run."""
    seed()
    httpx_mock.add_response(url=FILES, content=PDF, headers=PDF_HEADERS, is_reusable=True)
    httpx_mock.add_response(url=MANIFEST, json=manifest({"name": "no id"}), is_reusable=True)

    with pytest.raises(ManifestShapeError, match="resourceId"):
        fetch_pending(conn, settings, budget=0, max_attachments=1)

    assert conn.execute(
        "SELECT count(*) FROM attachments WHERE fetch_status = 'fetched'"
    ).fetchone() == (1,)
    assert conn.execute(
        "SELECT status FROM ingestion_runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone() == ("failed",)


def test_a_429_stops_the_stage(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings, seed: Seed
) -> None:
    """Rate limits on this endpoint are not published, so back off rather than keep going."""
    seed()
    httpx_mock.add_response(url=MANIFEST, status_code=429, is_reusable=True)

    result = fetch_pending(conn, settings, budget=0, max_attachments=0)

    assert result.manifests_failed == 1  # stopped after the first, not all five
    assert len(httpx_mock.get_requests(url=MANIFEST)) == 1


def test_files_the_manifest_says_are_unfetchable_are_skipped_without_a_request(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings, seed: Seed
) -> None:
    """The declared size and access flags arrive before the download, so nothing is spent."""
    seed()
    conn.execute("DELETE FROM attachments")
    httpx_mock.add_response(
        url=MANIFEST,
        json=manifest(
            entry("big", size=10_000_000),
            entry("controlled", exportControlled="1"),
            entry("restricted", accessStatus="restricted"),
            entry("ok"),
        ),
        is_reusable=True,
    )
    small = settings.model_copy(update={"max_attachment_bytes": 2048})

    result = fetch_pending(conn, small, budget=0, max_attachments=0, max_manifests=1)

    statuses = dict(conn.execute("SELECT filename, fetch_status FROM attachments").fetchall())
    assert statuses == {
        "big.pdf": "skipped",
        "controlled.pdf": "skipped",
        "restricted.pdf": "skipped",
        "ok.pdf": "pending",
    }
    assert result.attachments_found == 4
    assert not httpx_mock.get_requests(url=FILES)


def test_a_second_sighting_refreshes_metadata_and_keeps_the_fetched_status(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings, seed: Seed
) -> None:
    """Re-checks are idempotent on (notice_id, url): a fetched file is not re-queued."""
    seed()
    conn.execute("DELETE FROM attachments")
    httpx_mock.add_response(url=MANIFEST, json=manifest(entry("r1")), is_reusable=True)
    httpx_mock.add_response(url=FILES, content=PDF, headers=PDF_HEADERS, is_reusable=True)
    fetch_pending(conn, settings, budget=0, max_attachments=1, max_manifests=1)
    assert conn.execute("SELECT fetch_status FROM attachments").fetchone() == ("fetched",)

    # An amendment to the notice that was checked makes its manifest due again. It has to be
    # that same notice: with max_manifests=1 the queue would otherwise pick a different one.
    (checked,) = conn.execute(
        "SELECT notice_id FROM notices WHERE manifest_status = 'checked'"
    ).fetchone()
    conn.execute(
        "INSERT INTO notice_versions (notice_id, observed_at, raw_hash, raw_json)"
        " VALUES (?, '2099-01-01T00:00:00Z', 'h', '{}')",
        (checked,),
    )
    result = fetch_pending(conn, settings, budget=0, max_attachments=0, max_manifests=1)

    assert result.manifests_checked == 1 and result.attachments_found == 0
    assert conn.execute(
        "SELECT fetch_status, last_seen_at IS NOT NULL FROM attachments"
    ).fetchone() == (
        "fetched",
        1,
    )


def test_off_site_links_are_recorded_where_they_live_and_never_fetched(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings, seed: Seed
) -> None:
    """A manifest entry of type 'link' is a URL on another procurement portal, not a document
    SAM.gov holds. Recording it says where the solicitation actually is; fetching it would
    yield that portal's HTML, or a failure indistinguishable from a broken network (#5)."""
    seed()
    conn.execute("DELETE FROM attachments")
    link = {
        "resourceId": "l1",
        "type": "link",
        "size": 0,
        "description": "PIEE Solicitation Module Link",
        "uri": "https://piee.eb.mil/sol/xhtml/unauth/search/oppMgmtLink.xhtml",
        "accessStatus": "public",
        "exportControlled": "0",
        "deletedFlag": "0",
    }
    httpx_mock.add_response(url=MANIFEST, json=manifest(link, entry("r1")), is_reusable=True)
    lines: list[str] = []

    fetch_pending(conn, settings, budget=0, max_attachments=0, max_manifests=1, report=lines.append)

    rows = dict(conn.execute("SELECT url, fetch_status FROM attachments").fetchall())
    assert rows["https://piee.eb.mil/sol/xhtml/unauth/search/oppMgmtLink.xhtml"] == "skipped"
    assert any(u.endswith("/r1/download") and s == "pending" for u, s in rows.items())
    assert conn.execute(
        "SELECT filename FROM attachments WHERE fetch_status = 'skipped'"
    ).fetchone() == ("PIEE Solicitation Module Link",)
    assert "manifests: 1 live on portals orrery does not fetch" in lines
