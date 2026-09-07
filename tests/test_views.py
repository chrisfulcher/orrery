import sqlite3
from collections.abc import Callable

from mentor.ingest.awards import AwardsResult

Seed = Callable[[dict | None], None]
SeedAwards = Callable[[list[dict] | None], AwardsResult]

# Interface version 1. A view may gain columns; it must never lose or rename one.
V1_COLUMNS = {
    "v_notices": {
        "notice_id", "solicitation_number", "title", "notice_type", "naics_code", "psc_code",
        "set_aside_code", "posted_at", "response_deadline", "active", "first_seen_at",
        "last_seen_at", "source_id", "agency_path_code", "agency_path_name", "agency_entity_id",
        "agency", "description_status", "description", "url", "attachments",
        "attachments_fetched", "attachments_extracted", "versions",
        "award_number", "award_date", "award_amount", "awardee",
    },
    "v_contracts": {
        "contract_id", "award_key", "piid", "parent_piid", "awarding_entity_id",
        "awarding_office_code", "awarding_office", "vendor_entity_id", "vendor", "vendor_uei",
        "cage", "solicitation_identifier", "award_date", "last_action_date", "pop_start",
        "pop_end", "value_usd", "potential_value_usd", "naics_code", "psc_code",
        "award_type_code", "set_aside_code", "extent_competed_code", "source_id",
        "first_seen_at", "last_seen_at", "url",
    },
    "v_entities": {
        "entity_id", "kind", "name", "agency_path_code", "uei", "cage", "parent_entity_id",
        "parent", "first_seen_at", "last_seen_at", "notices",
    },
    "v_contractors": {
        "entity_id", "name", "uei", "cage", "first_seen_at", "last_seen_at", "awards",
        "awards_value_usd", "last_award_date", "registration_status", "registration_expires",
        "naics_primary",
    },
    "v_pursuits": {
        "user_id", "pursuit_id", "title", "summary", "stage", "pwin", "notes", "held_until",
        "outcome", "closed_at", "office_entity_id", "office", "office_code", "naics_code",
        "incumbent_contract_id", "incumbent", "incumbent_pop_end", "notices", "open_tasks",
        "next_due", "next_response_deadline", "last_event_at", "created_at", "updated_at",
    },
    "v_pursuit_tasks": {
        "user_id", "task_id", "pursuit_id", "pursuit_title", "pursuit_stage", "outcome",
        "closed_at", "stage", "title", "origin", "due", "done_at", "created_at",
    },
    "v_pipeline": {
        "user_id", "tracked_id", "notice_id", "stage", "stage_order", "pwin", "notes",
        "created_at", "updated_at", "title", "agency", "response_deadline", "active",
    },
    "v_quota_daily": {"day", "requests", "failed"},
}  # fmt: skip


def test_every_view_selects_and_keeps_its_v1_columns(conn: sqlite3.Connection, seed: Seed) -> None:
    seed()
    views = {name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'")}
    assert views == set(V1_COLUMNS)
    for view, expected in V1_COLUMNS.items():
        cursor = conn.execute(f"SELECT * FROM {view} LIMIT 1")
        actual = {column[0] for column in cursor.description}
        assert expected <= actual, f"{view} lost columns: {expected - actual}"


def test_v_notices_counts_and_url(conn: sqlite3.Connection, seed: Seed) -> None:
    seed()
    row = conn.execute(
        "SELECT agency, attachments, attachments_fetched, versions, url FROM v_notices"
        " WHERE agency_path_code = '075.7526.75R602'"
    ).fetchone()
    assert row[:4] == ("HRSA HEADQUARTERS", 1, 0, 1)
    assert row[4].startswith("https://sam.gov/workspace/contract/opp/")


def test_v_entities_counts_offices_below(conn: sqlite3.Connection, seed: Seed) -> None:
    seed()
    rows = dict(
        conn.execute(
            "SELECT agency_path_code, notices FROM v_entities WHERE agency_path_code LIKE '075%'"
        )
    )
    assert rows == {"075": 1, "075.7526": 1, "075.7526.75R602": 1}
    (parent,) = conn.execute(
        "SELECT parent FROM v_entities WHERE agency_path_code = '075.7526'"
    ).fetchone()
    assert parent == "HEALTH AND HUMAN SERVICES, DEPARTMENT OF"


def test_v_notices_award_columns(conn: sqlite3.Connection, seed: Seed) -> None:
    seed()
    rows = conn.execute(
        "SELECT award_number, award_date, award_amount, awardee FROM v_notices"
        " WHERE award_number IS NOT NULL"
    ).fetchall()
    assert rows == [("75S20326F80003", "2026-08-21", None, None)]


def test_v_contractors_sums_awards_and_reads_latest_facts(
    conn: sqlite3.Connection, seed_awards: SeedAwards
) -> None:
    seed_awards()
    (leidos,) = conn.execute("SELECT entity_id FROM entities WHERE uei = 'UE9QJD4KK1L6'").fetchone()
    for observed, status in (
        ("2026-01-01T00:00:00Z", "Active"),
        ("2026-06-01T00:00:00Z", "Expired"),
    ):
        conn.execute(
            "INSERT INTO facts (subject_type, subject_id, predicate, value_type, value, source_id,"
            " observed_at, confidence, extraction_method) VALUES ('entity', ?,"
            " 'sam.registration_status', 'text', ?, 'sam_entities', ?, 1.0, 'parse')",
            (str(leidos), status, observed),
        )
    rows = conn.execute(
        "SELECT name, awards, awards_value_usd, last_award_date, registration_status,"
        " registration_expires FROM v_contractors ORDER BY name"
    ).fetchall()
    assert rows == [
        ("CDW GOVERNMENT LLC", 1, 48000.0, "2024-01-15", None, None),
        ("LEIDOS, INC.", 1, 125000.5, "2025-08-01", "Expired", None),
    ]
