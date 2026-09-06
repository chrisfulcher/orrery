import hashlib
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


def _make_pdf(pages: list[str]) -> bytes:
    """A minimal PDF 1.4 (Helvetica, one text run per page) that pypdf extracts verbatim."""
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(pages)))
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, text in enumerate(pages):
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
            f" /Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>".encode()
        )
        objs.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content))
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


@pytest.fixture
def make_pdf() -> Callable[[list[str]], bytes]:
    return _make_pdf


MOST_LINKS_NOTICE = max(
    SEARCH_FIXTURE["opportunitiesData"], key=lambda r: len(r["resourceLinks"] or [])
)["noticeId"]


@pytest.fixture
def fetched(
    conn: sqlite3.Connection, settings: Settings, seed: Callable[[dict | None], None]
) -> Callable[..., int]:
    """Seed the fixture notices, then store bytes as a fetched attachment; returns its id."""
    seed()

    def _fetched(filename: str, data: bytes, *, notice_id: str = MOST_LINKS_NOTICE) -> int:
        (attachment_id,) = conn.execute(
            "SELECT attachment_id FROM attachments WHERE notice_id = ? AND fetch_status = 'pending'"
            " ORDER BY attachment_id LIMIT 1",
            (notice_id,),
        ).fetchone()
        sha = hashlib.sha256(data).hexdigest()
        relative = Path("attachments") / notice_id / f"{sha[:16]}-{filename}"
        (settings.data_dir / relative).parent.mkdir(parents=True, exist_ok=True)
        (settings.data_dir / relative).write_bytes(data)
        conn.execute(
            "UPDATE attachments SET filename = ?, path = ?, content_hash = ?, fetched_at = ?,"
            " fetch_status = 'fetched' WHERE attachment_id = ?",
            (filename, relative.as_posix(), sha, db.utcnow(), attachment_id),
        )
        return attachment_id

    return _fetched
