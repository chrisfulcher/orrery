import sqlite3
from collections.abc import Callable

import pytest
from conftest import EMBED_URL, register_fake_embeddings
from pytest_httpx import HTTPXMock

from orrery.config import Settings
from orrery.embed.client import EmbeddingError
from orrery.embed.pipeline import EmbedResult, embed_pending
from orrery.extract.text import extract_pending

Fetched = Callable[..., int]
MakePdf = Callable[[list[str]], bytes]


@pytest.fixture
def sources(
    conn: sqlite3.Connection, settings: Settings, fetched: Fetched, make_pdf: MakePdf
) -> tuple[str, int]:
    """One notice with a fetched description and one extracted two-page PDF on that notice."""
    attachment_id = fetched("sow.pdf", make_pdf(["Xylophone maintenance", "Second page"]))
    extract_pending(conn, settings)
    (notice_id,) = conn.execute(
        "SELECT notice_id FROM attachments WHERE attachment_id = ?", (attachment_id,)
    ).fetchone()
    conn.execute(
        "UPDATE notices SET description = 'A zeppelin hangar.', description_status = 'fetched'"
        " WHERE notice_id = ?",
        (notice_id,),
    )
    return notice_id, attachment_id


def rows(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute(
        "SELECT notice_id, attachment_id, chunk_index, page, model, length(vector), text"
        " FROM embeddings ORDER BY embedding_id"
    ).fetchall()


def test_embeds_descriptions_and_attachments(
    conn: sqlite3.Connection,
    settings: Settings,
    sources: tuple[str, int],
    fake_embeddings: list[list[str]],
) -> None:
    notice_id, attachment_id = sources

    result = embed_pending(conn, settings)

    assert result == EmbedResult(1, 1, 3, "nomic-embed-text")
    assert rows(conn) == [
        (notice_id, None, 0, None, "nomic-embed-text", 16, "A zeppelin hangar."),
        (notice_id, attachment_id, 0, 1, "nomic-embed-text", 16, "Xylophone maintenance"),
        (notice_id, attachment_id, 1, 2, "nomic-embed-text", 16, "Second page"),
    ]
    assert fake_embeddings == [["A zeppelin hangar."], ["Xylophone maintenance", "Second page"]]

    assert embed_pending(conn, settings) == EmbedResult(0, 0, 0, "nomic-embed-text")
    assert len(fake_embeddings) == 2


def test_limit_caps_sources(
    conn: sqlite3.Connection,
    settings: Settings,
    sources: tuple[str, int],
    fake_embeddings: list[list[str]],
) -> None:
    assert embed_pending(conn, settings, limit=1) == EmbedResult(1, 0, 1, "nomic-embed-text")
    assert embed_pending(conn, settings) == EmbedResult(0, 1, 2, "nomic-embed-text")


def test_endpoint_failure_leaves_no_partial_rows(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings, sources: tuple[str, int]
) -> None:
    httpx_mock.add_response(url=EMBED_URL, status_code=500)
    batches: list[list[str]] = []
    register_fake_embeddings(httpx_mock, batches)

    with pytest.raises(EmbeddingError):
        embed_pending(conn, settings)
    assert rows(conn) == []

    assert embed_pending(conn, settings) == EmbedResult(1, 1, 3, "nomic-embed-text")


def test_model_change_embeds_again(
    conn: sqlite3.Connection,
    settings: Settings,
    sources: tuple[str, int],
    fake_embeddings: list[list[str]],
) -> None:
    embed_pending(conn, settings)
    other = settings.model_copy(update={"embed_model": "other-model"})

    assert embed_pending(conn, other) == EmbedResult(1, 1, 3, "other-model")

    models = conn.execute("SELECT model, count(*) FROM embeddings GROUP BY model").fetchall()
    assert models == [("nomic-embed-text", 3), ("other-model", 3)]


def test_embed_reports_and_cancels_between_sources(
    conn: sqlite3.Connection, settings: Settings, sources: tuple[str, int], fake_embeddings: list
) -> None:
    from orrery.progress import JobCancelled

    notice_id, attachment_id = sources
    lines: list[str] = []
    with pytest.raises(JobCancelled):
        embed_pending(conn, settings, report=lines.append, cancelled=lambda: len(lines) >= 1)
    assert lines == [f"notice {notice_id}: 1 chunk(s)"]
    assert conn.execute("SELECT count(*) FROM embeddings").fetchone() == (1,)
    result = embed_pending(conn, settings)
    assert (result.notices, result.attachments) == (0, 1)
