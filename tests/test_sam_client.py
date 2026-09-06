import hashlib
import json
import logging
import re
import sqlite3
from datetime import date
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from mentor.config import Settings
from mentor.quota import BudgetExceeded
from mentor.sam.client import SamClient, SamError, _filename_from

FIXTURES = Path(__file__).with_name("fixtures")
DOWNLOAD_URL = "https://sam.gov/api/prod/opps/v3/opportunities/resources/files/abc/download"


def endpoints(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT endpoint FROM api_requests ORDER BY request_id").fetchall()
    return [endpoint for (endpoint,) in rows]


def page(records: list[dict], total: int, offset: int) -> dict:
    return {"totalRecords": total, "limit": 2, "offset": offset, "opportunitiesData": records}


def test_pagination_and_request_logging(
    httpx_mock: HTTPXMock, client: SamClient, conn: sqlite3.Connection
) -> None:
    httpx_mock.add_response(
        url=re.compile(r".*&offset=0&.*"), json=page([{"a": 1}, {"a": 2}], 3, 0)
    )
    httpx_mock.add_response(url=re.compile(r".*&offset=2&.*"), json=page([{"a": 3}], 3, 2))

    pages = list(client.search_pages(date(2026, 9, 5), date(2026, 9, 6), "541512", limit=2))

    assert [len(p.opportunities_data) for p in pages] == [2, 1]
    sent = httpx_mock.get_requests()
    assert sent[0].url.params["api_key"] == "test-key"
    assert sent[0].url.params["postedFrom"] == "09/05/2026"
    logged = endpoints(conn)
    assert len(logged) == 2
    assert "offset=0" in logged[0] and "offset=2" in logged[1]
    assert "postedFrom=09%2F05%2F2026" in logged[0]
    assert all("api_key" not in endpoint for endpoint in logged)


def test_window_longer_than_a_year_is_refused(
    httpx_mock: HTTPXMock, client: SamClient, conn: sqlite3.Connection
) -> None:
    with pytest.raises(ValueError):
        list(client.search_pages(date(2025, 1, 1), date(2026, 1, 2), "541512"))
    assert httpx_mock.get_requests() == []
    assert endpoints(conn) == []


def test_key_is_only_sent_to_the_api_host(
    httpx_mock: HTTPXMock, client: SamClient, conn: sqlite3.Connection
) -> None:
    with pytest.raises(SamError, match="refusing"):
        client.get_description("https://evil.example/noticedesc?noticeid=1")
    assert httpx_mock.get_requests() == []
    assert endpoints(conn) == []


def test_budget_refusal_spends_nothing(
    httpx_mock: HTTPXMock, settings: Settings, conn: sqlite3.Connection, run_id: int
) -> None:
    httpx_mock.add_response(json={"description": "<p>one</p>"})
    one = settings.model_copy(update={"sam_daily_budget": 1})
    with SamClient(one, conn, run_id) as client:
        client.get_description("https://api.sam.gov/prod/opportunities/v1/noticedesc?noticeid=1")
        with pytest.raises(BudgetExceeded):
            client.get_description(
                "https://api.sam.gov/prod/opportunities/v1/noticedesc?noticeid=2"
            )
    assert len(httpx_mock.get_requests()) == 1
    assert len(endpoints(conn)) == 1


def test_http_error_is_logged_and_counted(
    httpx_mock: HTTPXMock, client: SamClient, conn: sqlite3.Connection
) -> None:
    httpx_mock.add_response(status_code=500)
    with pytest.raises(SamError, match="HTTP 500") as excinfo:
        client.get_description("https://api.sam.gov/prod/opportunities/v1/noticedesc?noticeid=1")
    assert "test-key" not in str(excinfo.value)
    row = conn.execute("SELECT status_code, error FROM api_requests").fetchone()
    assert row == (500, None)


def test_transport_error_is_logged(
    httpx_mock: HTTPXMock,
    client: SamClient,
    conn: sqlite3.Connection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    httpx_mock.add_exception(httpx.ConnectError("boom"))
    caplog.set_level(logging.INFO)
    with pytest.raises(SamError, match="ConnectError: boom") as excinfo:
        client.get_description("https://api.sam.gov/prod/opportunities/v1/noticedesc?noticeid=1")
    assert "test-key" not in str(excinfo.value)
    assert "test-key" not in caplog.text
    row = conn.execute("SELECT status_code, error FROM api_requests").fetchone()
    assert row == (None, "ConnectError: boom")


def test_description_keeps_existing_query_string(
    httpx_mock: HTTPXMock, client: SamClient, conn: sqlite3.Connection
) -> None:
    body = json.loads((FIXTURES / "sam_noticedesc_v1.json").read_text())
    httpx_mock.add_response(json=body)
    url = "https://api.sam.gov/prod/opportunities/v1/noticedesc?noticeid=abc"

    text = client.get_description(url)

    assert text.startswith("<p>")
    sent = httpx_mock.get_request()
    assert sent.url.params["noticeid"] == "abc"
    assert sent.url.params["api_key"] == "test-key"
    assert endpoints(conn) == [url]


def test_download_is_public_and_uncounted(
    httpx_mock: HTTPXMock, client: SamClient, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    content = b"%PDF-1.4 hello"
    httpx_mock.add_response(
        url=DOWNLOAD_URL,
        content=content,
        headers={
            "Content-Disposition": "attachment; filename=Name+With+Plus+Signs.pdf",
            "Content-Type": "application/octet-stream",
        },
    )
    dest = tmp_path / "attachments"

    result = client.download(DOWNLOAD_URL, dest)

    assert result.filename == "Name With Plus Signs.pdf"
    assert result.path.name == f"{result.sha256[:16]}-Name With Plus Signs.pdf"
    assert result.path.read_bytes() == content
    assert result.sha256 == hashlib.sha256(content).hexdigest()
    assert result.size == len(content)
    assert "api_key" not in str(httpx_mock.get_request().url)
    assert endpoints(conn) == []
    assert list(dest.glob("*.part")) == []


def test_download_failure_leaves_no_file(
    httpx_mock: HTTPXMock, client: SamClient, tmp_path: Path
) -> None:
    httpx_mock.add_response(url=DOWNLOAD_URL, status_code=404)
    dest = tmp_path / "attachments"
    with pytest.raises(SamError, match="HTTP 404"):
        client.download(DOWNLOAD_URL, dest)
    assert list(dest.iterdir()) == []


def test_missing_key_is_refused(settings: Settings, conn: sqlite3.Connection, run_id: int) -> None:
    with pytest.raises(SamError, match="MENTOR_SAM_API_KEY"):
        SamClient(settings.model_copy(update={"sam_api_key": None}), conn, run_id)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("attachment; filename=plain.pdf", "plain.pdf"),
        ('attachment; filename="quoted name.pdf"', "quoted name.pdf"),
        ("attachment; filename*=UTF-8''caf%C3%A9.pdf", "café.pdf"),
        ("attachment; filename=../../.hidden", "hidden"),
        ("inline", "attachment"),
        (None, "attachment"),
    ],
)
def test_filename_from_content_disposition(header: str | None, expected: str) -> None:
    assert _filename_from(header) == expected
