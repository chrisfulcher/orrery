import json
import re
import sqlite3
from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from mentor import db, runs
from mentor.config import Settings
from mentor.ingest.notices import ingest_notices
from mentor.sam.client import SamClient

SEARCH_FIXTURE = json.loads(
    (Path(__file__).with_name("fixtures") / "sam_search_v2.json").read_text()
)
SEARCH_URL = re.compile(r".*/opportunities/v2/search.*")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "mentor.sqlite"


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    """A migrated connection to a fresh database file."""
    connection = db.connect(db_path)
    db.migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        sam_api_key="test-key",
        sam_daily_budget=10,
        naics=["541512"],
        fetch_delay=0.0,
    )


@pytest.fixture
def run_id(conn: sqlite3.Connection) -> int:
    return runs.start(conn)


@pytest.fixture
def client(settings: Settings, conn: sqlite3.Connection, run_id: int) -> Iterator[SamClient]:
    with SamClient(settings, conn, run_id) as sam:
        yield sam


@pytest.fixture
def seed(
    conn: sqlite3.Connection, settings: Settings, httpx_mock: HTTPXMock
) -> Callable[[dict | None], None]:
    """Ingest the search fixture (or a modified copy); spends 1 of the 10-request budget."""

    def _seed(payload: dict | None = None) -> None:
        httpx_mock.add_response(url=SEARCH_URL, json=payload or SEARCH_FIXTURE)
        ingest_notices(conn, settings, posted_from=date(2026, 9, 5), posted_to=date(2026, 9, 6))

    return _seed
