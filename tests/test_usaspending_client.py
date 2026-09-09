import json
import re
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from orrery.config import Settings
from orrery.usaspending.client import DownloadTicket, UsaspendingClient, UsaspendingError

FIXTURES = Path(__file__).with_name("fixtures")
TICKET_JSON = json.loads((FIXTURES / "usaspending_download_awards.json").read_text())
STATUS_JSON = json.loads((FIXTURES / "usaspending_download_status.json").read_text())
DOWNLOAD_URL = re.compile(r"https://api\.usaspending\.gov/api/v2/download/awards/")
STATUS_URL = re.compile(r"https://api\.usaspending\.gov/api/v2/download/status.*")


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def client(settings: Settings, sleeps: list[float]) -> UsaspendingClient:
    return UsaspendingClient(settings, sleep=sleeps.append)


def ticket() -> DownloadTicket:
    return DownloadTicket(
        TICKET_JSON["file_name"], TICKET_JSON["file_url"], TICKET_JSON["status_url"]
    )


def test_request_download_posts_filters_and_returns_ticket(
    client: UsaspendingClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=DOWNLOAD_URL, json=TICKET_JSON)
    result = client.request_awards_download({"naics_codes": {"require": ["541512"]}})
    assert result == ticket()
    body = json.loads(httpx_mock.get_request().content)
    assert body == {"filters": {"naics_codes": {"require": ["541512"]}}, "file_format": "csv"}


def test_request_download_rejects_an_unexpected_body(
    client: UsaspendingClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=DOWNLOAD_URL, json={"detail": "nope"})
    with pytest.raises(UsaspendingError, match="missing"):
        client.request_awards_download({})


def test_wait_polls_with_a_growing_delay(
    client: UsaspendingClient, httpx_mock: HTTPXMock, sleeps: list[float]
) -> None:
    httpx_mock.add_response(url=STATUS_URL, json={**STATUS_JSON, "status": "ready"})
    httpx_mock.add_response(url=STATUS_URL, json={**STATUS_JSON, "status": "running"})
    httpx_mock.add_response(url=STATUS_URL, json=STATUS_JSON)
    assert client.wait_until_ready(ticket()) == STATUS_JSON["total_rows"]
    assert sleeps == [0.0, 1.0]  # fetch_delay is 0 in tests; then 1, 2, 4 ... capped at 30
    assert len(httpx_mock.get_requests()) == 3


def test_wait_raises_on_failed_status(client: UsaspendingClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=STATUS_URL, json={**STATUS_JSON, "status": "failed", "message": "too many rows"}
    )
    with pytest.raises(UsaspendingError, match="too many rows"):
        client.wait_until_ready(ticket())


def test_wait_gives_up_after_max_wait(
    client: UsaspendingClient, httpx_mock: HTTPXMock, sleeps: list[float]
) -> None:
    httpx_mock.add_response(
        url=STATUS_URL, json={**STATUS_JSON, "status": "running"}, is_reusable=True
    )
    with pytest.raises(UsaspendingError, match="not ready after 1 s"):
        client.wait_until_ready(ticket(), max_wait=1.0)
    assert sleeps == [0.0, 1.0]


def test_download_streams_the_zip_and_leaves_no_partial_file(
    client: UsaspendingClient, httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    httpx_mock.add_response(url=TICKET_JSON["file_url"], content=b"PK\x05\x06zip")
    path = client.download(ticket(), tmp_path / "downloads")
    assert path == tmp_path / "downloads" / TICKET_JSON["file_name"]
    assert path.read_bytes() == b"PK\x05\x06zip"
    assert list((tmp_path / "downloads").glob("*.part")) == []


def test_download_failure_leaves_nothing(
    client: UsaspendingClient, httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    httpx_mock.add_response(url=TICKET_JSON["file_url"], status_code=503)
    with pytest.raises(UsaspendingError, match="HTTP 503"):
        client.download(ticket(), tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "url", ["https://example.com/status", "https://usaspending.gov.example.com/x"]
)
def test_other_hosts_are_refused_without_a_request(
    client: UsaspendingClient, httpx_mock: HTTPXMock, url: str
) -> None:
    with pytest.raises(UsaspendingError, match="refusing to contact"):
        client.wait_until_ready(DownloadTicket("f.zip", url, url))
    assert httpx_mock.get_requests() == []
