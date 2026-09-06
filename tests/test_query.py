import sqlite3
from collections.abc import Callable

import pytest

from mentor import query

Seed = Callable[[dict | None], None]


def set_text(conn: sqlite3.Connection, attachment_id: int, filename: str, text: str) -> None:
    conn.execute(
        "UPDATE attachments SET filename = ?, extracted_text = ?, extract_status = 'done',"
        " fetch_status = 'fetched' WHERE attachment_id = ?",
        (filename, text, attachment_id),
    )


def attachments_of(conn: sqlite3.Connection, notice_id: str) -> list[int]:
    rows = conn.execute(
        "SELECT attachment_id FROM attachments WHERE notice_id = ? ORDER BY attachment_id",
        (notice_id,),
    ).fetchall()
    return [attachment_id for (attachment_id,) in rows]


@pytest.fixture
def notice_with_links(conn: sqlite3.Connection, seed: Seed) -> str:
    seed()
    (notice_id,) = conn.execute(
        "SELECT notice_id FROM attachments GROUP BY notice_id ORDER BY count(*) DESC LIMIT 1"
    ).fetchone()
    return notice_id


def test_attachment_only_word_hits_with_filename_source(
    conn: sqlite3.Connection, notice_with_links: str
) -> None:
    first, second = attachments_of(conn, notice_with_links)[:2]
    set_text(conn, first, "sow.pdf", "The contractor shall provide xylophone maintenance.")
    set_text(conn, second, "amendment.pdf", "Xylophone tuning is included.")

    hits = query.search(conn, "xylophone")

    assert len(hits) == 1
    hit = hits[0]
    assert hit.notice_id == notice_with_links
    assert hit.source in {"sow.pdf", "amendment.pdf"}
    assert "[xylophone]" in hit.snippet.lower()
    assert hit.agency is not None and hit.response_deadline is not None


def test_notice_text_hit_and_agency_name(conn: sqlite3.Connection, notice_with_links: str) -> None:
    conn.execute(
        "UPDATE notices SET description = 'Requires a zeppelin hangar.' WHERE notice_id = ?",
        (notice_with_links,),
    )
    (leaf,) = conn.execute(
        "SELECT e.name FROM notices n JOIN entities e ON e.entity_id = n.agency_entity_id"
        " WHERE n.notice_id = ?",
        (notice_with_links,),
    ).fetchone()

    hits = query.search(conn, "zeppelin")

    assert [(h.notice_id, h.source, h.agency) for h in hits] == [
        (notice_with_links, "notice", leaf)
    ]


def test_plain_queries_fall_back_to_quoting(
    conn: sqlite3.Connection, notice_with_links: str
) -> None:
    set_text(
        conn, attachments_of(conn, notice_with_links)[0], "sow.pdf", "Section L/M and wi-fi survey"
    )

    assert [h.notice_id for h in query.search(conn, "wi-fi")] == [notice_with_links]
    assert [h.notice_id for h in query.search(conn, "section l/m")] == [notice_with_links]


def test_operator_characters_become_phrases(conn: sqlite3.Connection, seed: Seed) -> None:
    seed()
    assert query.search(conn, "(") == []


def test_empty_query_raises(conn: sqlite3.Connection, seed: Seed) -> None:
    seed()
    with pytest.raises(query.InvalidQuery):
        query.search(conn, "")


def test_limit_applies_per_notice(conn: sqlite3.Connection, seed: Seed) -> None:
    seed()
    conn.execute("UPDATE notices SET description = 'quokka habitat'")
    assert len(query.search(conn, "quokka")) == 5
    assert len(query.search(conn, "quokka", limit=2)) == 2


def test_rebuild_restores_a_dropped_index_row(
    conn: sqlite3.Connection, notice_with_links: str
) -> None:
    attachment_id = attachments_of(conn, notice_with_links)[0]
    set_text(conn, attachment_id, "sow.pdf", "yttrium supply")
    conn.execute(
        "INSERT INTO attachments_fts(attachments_fts, rowid, extracted_text)"
        " VALUES ('delete', ?, 'yttrium supply')",
        (attachment_id,),
    )
    assert query.search(conn, "yttrium") == []

    query.rebuild_search(conn)

    assert [h.notice_id for h in query.search(conn, "yttrium")] == [notice_with_links]
