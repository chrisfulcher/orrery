import hashlib
import json
import logging
import re
import sqlite3
from datetime import date
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock, IteratorStream

from orrery.config import Settings
from orrery.quota import BudgetExceeded
from orrery.sam.client import (
    AttachmentTooLarge,
    ManifestShapeError,
    NoticeUnknown,
    SamClient,
    SamError,
    _filename_from,
)

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


def test_keyed_calls_are_refused_without_a_key(
    settings: Settings, conn: sqlite3.Connection, run_id: int
) -> None:
    """The key gates the keyed half only. Constructing must work, so that the free half is
    reachable with no credentials at all."""
    keyless = settings.model_copy(update={"sam_api_key": None})
    with SamClient(keyless, conn, run_id) as client:
        with pytest.raises(SamError, match="ORRERY_SAM_API_KEY"):
            client.get_description("https://api.sam.gov/prod/opportunities/v1/noticedesc?n=1")
        with pytest.raises(SamError, match="ORRERY_SAM_API_KEY"):
            next(client.search_pages(date(2026, 9, 1), date(2026, 9, 2), "541512"))
        with pytest.raises(SamError, match="ORRERY_SAM_API_KEY"):
            client.get_entities(["UE9QJD4KK1L6"])
    assert conn.execute("SELECT count(*) FROM api_requests").fetchone() == (0,)


def test_attachments_download_without_a_key(
    httpx_mock: HTTPXMock, settings: Settings, conn: sqlite3.Connection, run_id: int, tmp_path: Path
) -> None:
    """The download half sends no key and writes no api_requests row, so it must work when
    none is configured -- this is what a keyless install rests on."""
    httpx_mock.add_response(url=DOWNLOAD_URL, content=b"%PDF-1.7 body")
    keyless = settings.model_copy(update={"sam_api_key": None})
    with SamClient(keyless, conn, run_id) as client:
        result = client.download(DOWNLOAD_URL, tmp_path / "attachments")
    assert result.size == len(b"%PDF-1.7 body")
    assert result.path.read_bytes() == b"%PDF-1.7 body"
    assert conn.execute("SELECT count(*) FROM api_requests").fetchone() == (0,)


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


def test_download_transport_error_is_sam_error(
    httpx_mock: HTTPXMock, client: SamClient, tmp_path: Path
) -> None:
    httpx_mock.add_exception(httpx.ConnectError("boom"), url=DOWNLOAD_URL)
    dest = tmp_path / "attachments"
    with pytest.raises(SamError, match="ConnectError: boom"):
        client.download(DOWNLOAD_URL, dest)
    assert list(dest.iterdir()) == []


def test_download_over_limit_streamed_raises_and_leaves_no_file(
    httpx_mock: HTTPXMock, settings: Settings, conn: sqlite3.Connection, run_id: int, tmp_path: Path
) -> None:
    httpx_mock.add_response(url=DOWNLOAD_URL, stream=IteratorStream([b"abc", b"def", b"ghi"]))
    small = settings.model_copy(update={"max_attachment_bytes": 5})
    dest = tmp_path / "attachments"
    with SamClient(small, conn, run_id) as client, pytest.raises(AttachmentTooLarge):
        client.download(DOWNLOAD_URL, dest)
    assert list(dest.iterdir()) == []


ENTITY_FIXTURE = json.loads((FIXTURES / "sam_entity_v3.json").read_text())
S3_URL = "https://falextracts.s3.amazonaws.com/Entity%20Registration/Public%20V2/SAM_PUBLIC_UTF-8_MONTHLY_V2_20260906.ZIP?X-Amz-Signature=abc"


def test_get_entities_is_keyed_counted_and_capped(
    httpx_mock: HTTPXMock, client: SamClient, conn: sqlite3.Connection
) -> None:
    httpx_mock.add_response(
        url=re.compile(r".*/entity-information/v3/entities.*"), json=ENTITY_FIXTURE
    )
    body = client.get_entities(["UE9QJD4KK1L6"])
    assert body["totalRecords"] == 1
    sent = httpx_mock.get_request()
    assert sent.url.params["api_key"] == "test-key"
    assert sent.url.params["ueiSAM"] == "UE9QJD4KK1L6"
    assert "pointsOfContact" not in sent.url.params["includeSections"]
    [logged] = endpoints(conn)
    assert "ueiSAM=UE9QJD4KK1L6" in logged and "api_key" not in logged
    with pytest.raises(ValueError):
        client.get_entities([f"UEI{i:09d}" for i in range(11)])
    assert len(httpx_mock.get_requests()) == 1


def test_entity_extract_follows_the_presigned_redirect_without_the_key(
    httpx_mock: HTTPXMock, client: SamClient, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    httpx_mock.add_response(
        url=re.compile(r".*/data-services/v1/extracts.*"),
        status_code=302,
        headers={"location": S3_URL},
    )
    httpx_mock.add_response(url=S3_URL, content=b"PK\x05\x06zip")
    path = client.download_entity_extract(tmp_path / "sam")
    assert path == tmp_path / "sam" / "SAM_PUBLIC_UTF-8_MONTHLY_V2_20260906.ZIP"
    assert path.read_bytes() == b"PK\x05\x06zip"
    first, second = httpx_mock.get_requests()
    assert first.url.params["fileType"] == "ENTITY" and first.url.params["api_key"] == "test-key"
    assert "api_key" not in second.url.params and second.url.host == "falextracts.s3.amazonaws.com"
    rows = conn.execute("SELECT status_code, endpoint FROM api_requests").fetchall()
    assert len(rows) == 1 and rows[0][0] == 302 and "api_key" not in rows[0][1]
    assert list((tmp_path / "sam").glob("*.part")) == []


def test_entity_extract_refuses_a_redirect_carrying_the_key(
    httpx_mock: HTTPXMock, client: SamClient, tmp_path: Path
) -> None:
    httpx_mock.add_response(
        url=re.compile(r".*/data-services/v1/extracts.*"),
        status_code=302,
        headers={"location": "https://example.com/x.zip?api_key=test-key"},
    )
    with pytest.raises(SamError, match="refusing to follow"):
        client.download_entity_extract(tmp_path)
    assert len(httpx_mock.get_requests()) == 1


MANIFEST_URL = "https://sam.gov/api/prod/opps/v3/opportunities/N1/resources"


def manifest_body(*attachments: dict) -> dict:
    """The shape the endpoint returns when a notice has files (probe request 2)."""
    return {"_embedded": {"opportunityAttachmentList": [{"attachments": list(attachments)}]}}


ENTRY = {
    "resourceId": "abc",
    "name": "Statement of Work.pdf",
    "mimeType": ".pdf",
    "size": 261041,
    "accessStatus": "public",
    "exportControlled": "0",
    "deletedFlag": "0",
}


def keyless(settings: Settings) -> Settings:
    return settings.model_copy(update={"sam_api_key": None})


def test_manifest_is_read_without_a_key_and_spends_nothing(
    httpx_mock: HTTPXMock, settings: Settings, conn: sqlite3.Connection, run_id: int
) -> None:
    httpx_mock.add_response(url=MANIFEST_URL, json=manifest_body(ENTRY))
    with SamClient(keyless(settings), conn, run_id) as client:
        (item,) = client.get_attachment_manifest("N1")
        assert client.attachment_url(item.resource_id) == DOWNLOAD_URL
    assert (item.name, item.mime_type, item.size) == ("Statement of Work.pdf", ".pdf", 261041)
    assert item.public and not item.deleted
    assert conn.execute("SELECT count(*) FROM api_requests").fetchone() == (0,)
    assert httpx_mock.get_requests()[0].headers["Accept"] == "application/hal+json"


def test_a_notice_with_no_attachments_is_an_empty_list_not_an_error(
    httpx_mock: HTTPXMock, settings: Settings, conn: sqlite3.Connection, run_id: int
) -> None:
    """The endpoint omits _embedded entirely rather than sending an empty one (probe 3). This
    is a result, and recording it is what stops the notice being asked about forever."""
    httpx_mock.add_response(url=MANIFEST_URL, json={"_links": {}})
    with SamClient(keyless(settings), conn, run_id) as client:
        assert client.get_attachment_manifest("N1") == []


def test_an_unknown_notice_is_a_400_and_is_terminal(
    httpx_mock: HTTPXMock, settings: Settings, conn: sqlite3.Connection, run_id: int
) -> None:
    """SAM.gov answers an unknown id with 400, not 404 (probe 4)."""
    httpx_mock.add_response(
        url=MANIFEST_URL, status_code=400, json={"errors": {"details": "Record not found"}}
    )
    with SamClient(keyless(settings), conn, run_id) as client:
        with pytest.raises(NoticeUnknown, match="N1"):
            client.get_attachment_manifest("N1")


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("not json at all", "not JSON"),
        ('["a list"]', "not an object"),
    ],
)
def test_a_body_that_is_not_the_pinned_shape_is_a_shape_error(
    httpx_mock: HTTPXMock,
    settings: Settings,
    conn: sqlite3.Connection,
    run_id: int,
    body: str,
    match: str,
) -> None:
    httpx_mock.add_response(url=MANIFEST_URL, content=body.encode())
    with SamClient(keyless(settings), conn, run_id) as client:
        with pytest.raises(ManifestShapeError, match=match):
            client.get_attachment_manifest("N1")


def test_an_entry_without_a_resource_id_is_a_shape_error(
    httpx_mock: HTTPXMock, settings: Settings, conn: sqlite3.Connection, run_id: int
) -> None:
    """The resource id is the only field the row cannot be built without."""
    httpx_mock.add_response(url=MANIFEST_URL, json=manifest_body({"name": "orphan.pdf"}))
    with SamClient(keyless(settings), conn, run_id) as client:
        with pytest.raises(ManifestShapeError, match="resourceId"):
            client.get_attachment_manifest("N1")


def test_access_flags_are_strings_and_are_read_as_flags(
    httpx_mock: HTTPXMock, settings: Settings, conn: sqlite3.Connection, run_id: int
) -> None:
    """exportControlled and deletedFlag arrive as "0"/"1", not booleans."""
    controlled = {**ENTRY, "resourceId": "c1", "exportControlled": "1"}
    deleted = {**ENTRY, "resourceId": "d1", "deletedFlag": "1"}
    restricted = {**ENTRY, "resourceId": "r1", "accessStatus": "restricted"}
    httpx_mock.add_response(url=MANIFEST_URL, json=manifest_body(controlled, deleted, restricted))
    with SamClient(keyless(settings), conn, run_id) as client:
        items = {i.resource_id: i for i in client.get_attachment_manifest("N1")}
    assert items["c1"].export_controlled and not items["c1"].public
    assert items["d1"].deleted
    assert not items["r1"].public
