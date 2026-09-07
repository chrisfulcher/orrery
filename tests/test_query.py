import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import SEARCH_FIXTURE, fake_vector

from mentor import db, query
from mentor.config import Settings
from mentor.embed.client import pack
from mentor.embed.pipeline import embed_pending
from mentor.ingest.awards import AwardsResult

Seed = Callable[[dict | None], None]
SeedAwards = Callable[[list[dict] | None], AwardsResult]


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


def test_notice_detail_carries_award_fields_incumbent_history_and_officials(
    conn: sqlite3.Connection, seed_awards: SeedAwards
) -> None:
    seed_awards()
    hrsa = SEARCH_FIXTURE["opportunitiesData"][0]

    detail = query.notice(conn, hrsa["noticeId"])

    assert detail is not None
    assert (detail.award_number, detail.award_date) == ("75S20326F80003", "2026-08-21")
    assert detail.incumbent is not None and detail.incumbent.piid == "75R60222F00009"
    assert (
        detail.incumbent.vendor == "LEIDOS, INC." and detail.incumbent.vendor_uei == "UE9QJD4KK1L6"
    )
    assert [c.piid for c in detail.award_history] == ["75R60222F00009", "75R60224F00021"]
    assert detail.award_history[0].awarding_office == "HRSA HEADQUARTERS"
    assert detail.award_history[1].value_usd == 48000.0
    assert detail.officials == (
        query.Official(
            "Point of Contact 1", "primary", None, "poc1@example.gov", None, None,
            "ROCKVILLE MD 20852", 0,
        ),
    )  # fmt: skip
    army = SEARCH_FIXTURE["opportunitiesData"][1]
    assert query.notice(conn, army["noticeId"]).incumbent is None


def test_incumbent_falls_back_to_the_award_number(
    conn: sqlite3.Connection, seed_awards: SeedAwards
) -> None:
    seed_awards([{"award_id_piid": "75S20326F80003", "solicitation_identifier": "OTHER"}])
    detail = query.notice(conn, SEARCH_FIXTURE["opportunitiesData"][0]["noticeId"])
    assert detail is not None and detail.incumbent is not None
    assert detail.incumbent.piid == "75S20326F80003"


def test_officials_from_an_extract_row_count_other_notices(
    conn: sqlite3.Connection, settings: Settings, seed_awards: SeedAwards, tmp_path: Path
) -> None:
    from conftest import make_extract

    from mentor.ingest.bulk import ingest_bulk

    seed_awards()
    path = tmp_path / "extract.csv"
    path.write_bytes(
        make_extract(
            [
                {
                    "NoticeId": "b" * 32,
                    "PrimaryContactTitle": "Contracting Officer",
                    "PrimaryContactPhone": "301-555-0100",
                    "SecondaryContactFullname": "Point of Contact 2",
                    "SecondaryContactEmail": "poc2@example.gov",
                }
            ]
        )
    )
    ingest_bulk(conn, settings, path)

    detail = query.notice(conn, "b" * 32)

    assert detail is not None
    assert detail.officials == (
        query.Official("Point of Contact 1", "primary", "Contracting Officer", "poc1@example.gov",
                       "301-555-0100", None, "ROCKVILLE MD 20852", 1),
        query.Official("Point of Contact 2", "secondary", None, "poc2@example.gov", None, None,
                       "ROCKVILLE MD 20852", 0),
    )  # fmt: skip
    hrsa = query.notice(conn, SEARCH_FIXTURE["opportunitiesData"][0]["noticeId"])
    assert hrsa is not None and hrsa.officials[0].other_notices == 1


def test_awards_filters_and_order(conn: sqlite3.Connection, seed_awards: SeedAwards) -> None:
    seed_awards()
    assert [c.piid for c in query.awards(conn)] == ["75R60222F00009", "75R60224F00021"]
    assert [c.piid for c in query.awards(conn, limit=1)] == ["75R60222F00009"]
    assert [c.piid for c in query.awards(conn, uei="PHZDZ8SJ5CM1")] == ["75R60224F00021"]
    assert query.awards(conn, office_code="75R602", naics="541511") == []
    hrsa = SEARCH_FIXTURE["opportunitiesData"][0]["solicitationNumber"]
    assert [c.piid for c in query.awards(conn, solicitation=hrsa)] == ["75R60222F00009"]
    assert query.awards(conn, office_code="ZZZZZZ") == []


def test_contractor_and_office_award_summaries(
    conn: sqlite3.Connection, seed_awards: SeedAwards
) -> None:
    seed_awards()
    vendor = query.contractor(conn, "UE9QJD4KK1L6")
    assert vendor is not None and vendor.kind == "contractor"
    assert (vendor.uei, vendor.cage, vendor.notices) == ("UE9QJD4KK1L6", "5UTE1", 0)
    assert vendor.aliases == ("LEIDOS, INC.",) and vendor.chain == (
        query.EntityRef(vendor.entity_id, "LEIDOS, INC.", None),
    )
    assert (vendor.awards_count, vendor.awards_value_usd) == (1, 125000.5)
    assert vendor.awards[0].awarding_office_code == "75R602" and vendor.facts == ()
    assert query.contractor(conn, "NOPE") is None

    (office_id,) = conn.execute(
        "SELECT entity_id FROM entities WHERE agency_path_code = '075.7526.75R602'"
    ).fetchone()
    office = query.entity(conn, office_id)
    assert office is not None and office.uei is None
    assert (office.awards_count, office.awards_value_usd) == (2, 173000.5)
    assert [c.vendor for c in office.awards] == ["LEIDOS, INC.", "CDW GOVERNMENT LLC"]


def test_contractor_facts_and_their_summary(
    conn: sqlite3.Connection, seed_registrations: Callable[[], object]
) -> None:
    seed_registrations()
    detail = query.contractor(conn, "UE9QJD4KK1L6")
    assert detail is not None and detail.facts and detail.facts[0].source_id == "sam_entities"
    summary = dict(query.summarize_facts(detail.facts, max_items=2))
    assert summary["sam.registration_status"] == "Active"
    assert summary["sam.registration_expires"] == "2027-04-17"
    assert summary["sam.naics"].startswith("236220, 332311, … ") and summary["sam.naics"].endswith(
        " in all"
    )
    assert "sam.dba_name" not in summary
    older = query.Fact(
        "sam.registration_status", "Expired", "text", "2020-01-01T00:00:00Z", "sam_entities", None
    )
    assert (
        dict(query.summarize_facts((*detail.facts, older)))["sam.registration_status"] == "Active"
    )
    newest = detail.facts[0].observed_at
    twin = query.Fact("sam.business_type", "2X", "text", newest, "sam_entities", "api:v3")
    merged = dict(query.summarize_facts((twin, *detail.facts)))["sam.business_type"].split(", ")
    assert sorted(merged) == ["2X", "MF"]  # no duplicate for the value both sources carry
