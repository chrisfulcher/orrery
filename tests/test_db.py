import shutil
import sqlite3
from pathlib import Path

import pytest

from mentor import db

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
    "users",
}

MIGRATIONS = [
    "0001_initial.sql",
    "0002_description_queue.sql",
    "0003_extraction_and_search.sql",
    "0004_embeddings.sql",
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
    assert sources == [("sam_bulk_csv",), ("sam_opportunities_api",)]
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
