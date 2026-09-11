import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

from orrery import db, query

EXPECTED_TABLES = {
    "sources",
    "entities",
    "entity_aliases",
    "people",
    "contracts",
    "facts",
    "notices",
    "notice_versions",
    "ingestion_runs",
    "api_requests",
    "attachments",
    "naics_codes",
    "psc_codes",
    "notices_fts",
    "attachments_fts",
    "embeddings",
    "notice_summaries",
    "users",
    "saved_searches",
    "tracked_opportunities",
    "tracked_opportunity_events",
    "company_profiles",
    "entity_registrations",
    "workspace_documents",
    "pursuits",
    "pursuit_notices",
    "pursuit_tasks",
    "pursuit_events",
    "assessments",
}

MIGRATIONS = [
    "0001_initial.sql",
    "0002_description_queue.sql",
    "0003_extraction_and_search.sql",
    "0004_embeddings.sql",
    "0005_workspace.sql",
    "0006_views.sql",
    "0007_awards.sql",
    "0008_registrations.sql",
    "0009_documents.sql",
    "0010_pursuits.sql",
    "0011_recompetes.sql",
    "0012_assessments.sql",
    "0013_notice_summaries.sql",
    "0014_run_filter.sql",
    "0015_attachment_manifest.sql",
    "0016_extract_provenance.sql",
]
NOTICE_COLUMNS = "(notice_id, title, first_seen_at, last_seen_at, source_id, raw_json)"
NOW = "2026-01-01T00:00:00Z"
SOURCE = "sam_opportunities_api"


def insert_notice(conn: sqlite3.Connection, notice_id: str, title: str) -> None:
    conn.execute(
        f"INSERT INTO notices {NOTICE_COLUMNS} VALUES (?, ?, ?, ?, ?, '{{}}')",
        (notice_id, title, NOW, NOW, SOURCE),
    )


def fts_hits(conn: sqlite3.Connection, query: str) -> list[str]:
    rows = conn.execute(
        "SELECT notices.notice_id FROM notices_fts"
        " JOIN notices ON notices.id = notices_fts.rowid WHERE notices_fts MATCH ?",
        (query,),
    ).fetchall()
    return [notice_id for (notice_id,) in rows]


def test_migrate_is_idempotent(conn: sqlite3.Connection) -> None:
    assert db.migrate(conn) == []
    (count,) = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()
    assert count == len(MIGRATIONS)


def test_status_lists_applied_migrations(conn: sqlite3.Connection) -> None:
    assert db.status(conn) == db.Status(applied=MIGRATIONS, pending=[])


def test_status_on_unmigrated_database_writes_nothing(db_path: Path) -> None:
    conn = db.connect(db_path)
    assert db.status(conn) == db.Status(applied=[], pending=MIGRATIONS)
    tables = conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    assert tables == (0,)


def test_expected_tables_exist(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    assert EXPECTED_TABLES <= {name for (name,) in rows}


def test_wal_and_foreign_keys_enabled(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)


def test_seed_rows(conn: sqlite3.Connection) -> None:
    sources = conn.execute("SELECT source_id FROM sources ORDER BY source_id").fetchall()
    assert sources == [
        ("sam_bulk_csv",),
        ("sam_entities",),
        ("sam_opportunities_api",),
        ("usaspending_awards",),
    ]
    assert conn.execute("SELECT user_id, name FROM users").fetchall() == [(1, "local")]


def test_foreign_keys_enforced(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO attachments (notice_id, url) VALUES ('missing', 'https://x')")


def test_fts_follows_notice_changes(conn: sqlite3.Connection) -> None:
    insert_notice(conn, "n1", "Widget maintenance services")
    assert fts_hits(conn, "widget") == ["n1"]

    conn.execute("UPDATE notices SET title = 'Gadget repair' WHERE notice_id = 'n1'")
    assert fts_hits(conn, "widget") == []
    assert fts_hits(conn, "gadget") == ["n1"]

    conn.execute("DELETE FROM notices WHERE notice_id = 'n1'")
    assert fts_hits(conn, "gadget") == []


def test_facts_are_append_only(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO facts (subject_type, subject_id, predicate, value_type, value, source_id,"
        " observed_at, confidence, extraction_method)"
        " VALUES ('notice', 'n1', 'naics', 'text', '541512', 'sam_opportunities_api', ?, 1.0,"
        " 'parse')",
        (NOW,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE facts SET value = '541511'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM facts")


def test_unresolved_alias_fields_are_null_together(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO entity_aliases (alias, source_id) VALUES ('SOME OFFICE', ?)", (SOURCE,)
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO entity_aliases (alias, source_id, method)"
            " VALUES ('OTHER OFFICE', ?, 'exact_key')",
            (SOURCE,),
        )


def test_failed_migration_rolls_back(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_bad.sql").write_text(
        "CREATE TABLE half_done (x INTEGER CHECK (x > 0));\nINSERT INTO half_done VALUES (-1);\n"
    )
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations)
    conn = db.connect(db_path)

    with pytest.raises(sqlite3.IntegrityError):
        db.migrate(conn)

    assert db.applied(conn) == []
    rows = conn.execute("SELECT name FROM sqlite_master WHERE name = 'half_done'").fetchall()
    assert rows == []


def test_fts_migration_over_existing_rows(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0003 builds attachments_fts over existing rows; updating them must not corrupt it."""
    real = db.MIGRATIONS_DIR
    staged = tmp_path / "migrations"
    staged.mkdir()
    for name in MIGRATIONS[:2]:
        shutil.copy(real / name, staged / name)
    monkeypatch.setattr(db, "MIGRATIONS_DIR", staged)
    conn = db.connect(db_path)
    db.migrate(conn)
    insert_notice(conn, "n1", "Title")
    conn.execute("INSERT INTO attachments (notice_id, url) VALUES ('n1', 'https://x/1')")

    shutil.copy(real / MIGRATIONS[2], staged / MIGRATIONS[2])
    db.migrate(conn)
    conn.execute("UPDATE attachments SET extracted_text = 'xenon lamp', extract_status = 'done'")

    rows = conn.execute(
        "SELECT rowid FROM attachments_fts WHERE attachments_fts MATCH 'xenon'"
    ).fetchall()
    assert rows == [(1,)]


def test_load_vec_registers_vector_functions(conn: sqlite3.Connection) -> None:
    db.load_vec(conn)
    (version,) = conn.execute("SELECT vec_version()").fetchone()
    assert version.startswith("v")


def test_load_vec_unavailable_leaves_keyword_search_working(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "sqlite_vec", None)
    with pytest.raises(db.VecUnavailable):
        db.load_vec(conn)
    assert query.search(conn, "anything") == []


def test_tracked_events_are_append_only(conn: sqlite3.Connection) -> None:
    insert_notice(conn, "n1", "Title")
    conn.execute(
        "INSERT INTO tracked_opportunities (user_id, notice_id, stage) VALUES (1, 'n1', 'watching')"
    )
    conn.execute(
        "INSERT INTO tracked_opportunity_events (tracked_id, user_id, changed_at, field, new_value)"
        " VALUES (1, 1, ?, 'stage', 'watching')",
        (NOW,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE tracked_opportunity_events SET new_value = 'bid'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM tracked_opportunity_events")
    columns = [row[1] for row in conn.execute("PRAGMA table_info(attachments)").fetchall()]
    assert "priority" not in columns


def test_pursuits_migration_converts_tracked_opportunities(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0010 turns every tracked row into a pursuit with the same id, its notice, and its events."""
    real = db.MIGRATIONS_DIR
    staged = tmp_path / "migrations"
    staged.mkdir()
    for name in MIGRATIONS[:9]:
        shutil.copy(real / name, staged / name)
    monkeypatch.setattr(db, "MIGRATIONS_DIR", staged)
    conn = db.connect(db_path)
    db.migrate(conn)
    for notice_id, kind, path in (
        ("n1", "Sources Sought", "075.7526.75R602"),
        ("n2", "Solicitation", "021.2100.AMC.W91QF0"),
        ("n3", "Award Notice", None),
    ):
        insert_notice(conn, notice_id, f"Title {notice_id}")
        conn.execute(
            "UPDATE notices SET notice_type = ?, full_parent_path_code = ?, naics_code = '541512'"
            " WHERE notice_id = ?",
            (kind, path, notice_id),
        )
    rows = [
        ("n1", "watching", None, NOW),
        ("n2", "won", 80, NOW),
        ("n3", "no-bid", 10, "2026-02-02T00:00:00Z"),
    ]
    for notice_id, stage, pwin, updated in rows:
        conn.execute(
            "INSERT INTO tracked_opportunities (user_id, notice_id, stage, pwin, created_at,"
            " updated_at) VALUES (1, ?, ?, ?, ?, ?)",
            (notice_id, stage, pwin, NOW, updated),
        )
    conn.execute(
        "INSERT INTO tracked_opportunity_events (tracked_id, user_id, changed_at, field, old_value,"
        " new_value) VALUES (2, 1, ?, 'stage', 'pursuing', 'won'), (2, 1, ?, 'pwin', '40', '80')",
        (NOW, NOW),
    )

    shutil.copy(real / MIGRATIONS[9], staged / MIGRATIONS[9])
    db.migrate(conn)

    pursuits = conn.execute(
        "SELECT pursuit_id, stage, outcome, closed_at, office_code, pwin, notices, last_event_at"
        " FROM v_pursuits ORDER BY pursuit_id"
    ).fetchall()
    assert pursuits == [
        (1, "identify", None, None, "75R602", None, 1, None),
        (2, "post-award", "won", None, "W91QF0", 80, 1, NOW),
        (3, "identify", "no-bid", "2026-02-02T00:00:00Z", None, 10, 1, None),
    ]
    roles = conn.execute(
        "SELECT pursuit_id, role FROM pursuit_notices ORDER BY pursuit_id"
    ).fetchall()
    assert roles == [(1, "sources-sought"), (2, "solicitation"), (3, "award")]
    events = conn.execute(
        "SELECT pursuit_id, field, old_value, new_value FROM pursuit_events ORDER BY event_id"
    ).fetchall()
    assert events == [(2, "stage", "pursuing", "won"), (2, "pwin", "40", "80")]
    assert conn.execute("SELECT count(*) FROM v_pipeline").fetchone() == (3,)  # legacy view intact
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE pursuit_events SET note = 'x'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM pursuit_events")


def test_recompetes_migration_backfills_the_potential_end(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = db.MIGRATIONS_DIR
    staged = tmp_path / "migrations"
    staged.mkdir()
    for name in MIGRATIONS[:10]:
        shutil.copy(real / name, staged / name)
    monkeypatch.setattr(db, "MIGRATIONS_DIR", staged)
    conn = db.connect(db_path)
    db.migrate(conn)
    conn.execute(
        "INSERT INTO contracts (award_key, piid, pop_end, source_id, first_seen_at, last_seen_at,"
        " raw_json) VALUES ('K1', 'P1', '2026-06-30', 'usaspending_awards', ?, ?,"
        ' \'{"period_of_performance_potential_end_date": "2028-06-30 00:00:00"}\'),'
        " ('K2', 'P2', '2026-01-31', 'usaspending_awards', ?, ?, '{}')",
        (NOW, NOW, NOW, NOW),
    )
    shutil.copy(real / MIGRATIONS[10], staged / MIGRATIONS[10])
    db.migrate(conn)
    rows = conn.execute("SELECT piid, pop_potential_end FROM v_contracts ORDER BY piid").fetchall()
    assert rows == [("P1", "2028-06-30"), ("P2", None)]
