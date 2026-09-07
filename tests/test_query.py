import sqlite3
from collections.abc import Callable

import pytest
from conftest import SEARCH_FIXTURE, fake_vector

from mentor import db, query
from mentor.config import Settings
from mentor.embed.client import pack
from mentor.embed.pipeline import embed_pending

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


def test_semantic_search_ranks_by_meaning(
    conn: sqlite3.Connection,
    settings: Settings,
    notice_with_links: str,
    fake_embeddings: list[list[str]],
) -> None:
    set_text(conn, attachments_of(conn, notice_with_links)[0], "sow.pdf", "Xylophone upkeep")
    (other,) = conn.execute(
        "SELECT notice_id FROM notices WHERE notice_id <> ? LIMIT 1", (notice_with_links,)
    ).fetchone()
    conn.execute(
        "UPDATE notices SET description = 'Zeppelin hangar', description_status = 'fetched'"
        " WHERE notice_id = ?",
        (other,),
    )
    embed_pending(conn, settings)
    db.load_vec(conn)

    hits = query.semantic_search(conn, pack(fake_vector("xylophone")), model=settings.embed_model)

    assert [h.notice_id for h in hits] == [notice_with_links, other]
    assert hits[0].source == "sow.pdf" and hits[0].page == 1
    assert hits[1].source == "notice" and hits[1].page is None
    assert hits[0].rank < hits[1].rank
    assert "[" not in hits[0].snippet and len(hits[0].snippet) <= 203
    assert len(query.semantic_search(conn, pack([1.0, 0, 0, 1.0]), model="other", limit=5)) == 0
    assert (
        len(
            query.semantic_search(conn, pack([1.0, 0, 0, 1.0]), model=settings.embed_model, limit=1)
        )
        == 1
    )


def test_search_filters_narrow_hits(conn: sqlite3.Connection, seed: Seed) -> None:
    seed()
    conn.execute("UPDATE notices SET description = 'quokka habitat'")
    assert len(query.search(conn, "quokka")) == 5
    assert len(query.search(conn, "quokka", filters=query.Filters(set_asides=("SBA",)))) == 2


def test_list_notices_orders_by_deadline(conn: sqlite3.Connection, seed: Seed) -> None:
    seed()
    hits = query.list_notices(conn, query.Filters())
    deadlines = [h.response_deadline for h in hits]
    assert deadlines[:-1] == sorted(deadlines[:-1]) and deadlines[-1] is None
    assert all(h.rank == 0.0 and h.source == "notice" for h in hits)
    assert len(query.list_notices(conn, query.Filters(), limit=2)) == 2


def test_notice_detail(conn: sqlite3.Connection, seed: Seed) -> None:
    seed()
    hrsa = SEARCH_FIXTURE["opportunitiesData"][0]

    detail = query.notice(conn, hrsa["noticeId"])

    assert detail is not None
    assert detail.title == hrsa["title"] and detail.active is True
    assert [ref.name for ref in detail.agency_chain] == [
        "HEALTH AND HUMAN SERVICES, DEPARTMENT OF",
        "HEALTH RESOURCES AND SERVICES ADMINISTRATION",
        "HRSA HEADQUARTERS",
    ]
    assert detail.agency == "HRSA HEADQUARTERS" and detail.url == hrsa["uiLink"]
    assert detail.versions == 1 and len(detail.attachments) == 1
    assert detail.attachments[0].fetch_status == "pending" and detail.attachments[0].text_chars == 0
    assert query.notice(conn, "nope") is None


def test_entity_detail(conn: sqlite3.Connection, seed: Seed) -> None:
    seed()
    hrsa = SEARCH_FIXTURE["opportunitiesData"][0]["noticeId"]
    (root_id,) = conn.execute(
        "SELECT entity_id FROM entities WHERE agency_path_code = '075'"
    ).fetchone()
    (leaf_id,) = conn.execute(
        "SELECT entity_id FROM entities WHERE agency_path_code = '075.7526.75R602'"
    ).fetchone()

    root = query.entity(conn, root_id)
    leaf = query.entity(conn, leaf_id)

    assert root is not None and leaf is not None
    assert root.kind == "agency" and root.parent is None and len(root.chain) == 1
    assert [child.path_code for child in root.children] == ["075.7526"]
    assert root.notices == 1 and root.recent[0].notice_id == hrsa
    assert len(leaf.chain) == 3 and leaf.parent is not None and leaf.parent.path_code == "075.7526"
    assert leaf.aliases == ("HRSA HEADQUARTERS",) and leaf.children == ()
    assert query.entity(conn, 999) is None


def test_activity_and_quota_series(
    conn: sqlite3.Connection, seed: Seed, run_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "utcnow", lambda: "2026-09-06T12:00:00Z")
    seed()
    conn.execute(
        "INSERT INTO api_requests (run_id, endpoint, requested_at)"
        " VALUES (?, 'https://api.sam.gov/x', '2026-09-05T01:00:00Z')",
        (run_id,),
    )

    activity = query.activity(conn, days=3)
    quota = query.quota_history(conn, days=3)

    assert activity == [("2026-09-04", 0), ("2026-09-05", 0), ("2026-09-06", 5)]
    assert quota[:2] == [("2026-09-04", 0), ("2026-09-05", 1)]
    assert query.upcoming(conn, days=365)[0].response_deadline is not None
    assert query.counts(conn) == query.StoreCounts(notices=5, active=5, entities=19)
