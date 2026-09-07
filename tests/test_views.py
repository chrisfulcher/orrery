import sqlite3
from collections.abc import Callable

Seed = Callable[[dict | None], None]

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
