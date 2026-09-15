"""The shipped NAICS and PSC code lists, and the loader that puts them in a store."""

import shutil
import sqlite3
from pathlib import Path

import pytest

from orrery import db, reference

NOW = "2026-01-01T00:00:00Z"
TERMS = "U.S. Government work, public domain; shipped with orrery"


def counts(conn: sqlite3.Connection) -> tuple[int, int]:
    naics = conn.execute("SELECT count(*) FROM naics_codes").fetchone()[0]
    psc = conn.execute("SELECT count(*) FROM psc_codes").fetchone()[0]
    return naics, psc


def test_migrate_loads_both_lists(conn: sqlite3.Connection) -> None:
    naics, psc = counts(conn)
    assert naics > 2000
    assert psc > 1000
    assert conn.execute("SELECT title FROM naics_codes WHERE code = '541512'").fetchone() == (
        "Computer Systems Design Services",
    )
    assert conn.execute("SELECT title FROM psc_codes WHERE code = 'DA01'").fetchone() == (
        "IT AND TELECOM - BUSINESS APPLICATION/APPLICATION DEVELOPMENT SUPPORT SERVICES (LABOR)",
    )


def test_sector_ranges_keep_the_census_spelling(conn: sqlite3.Connection) -> None:
    """31-33, 44-45 and 48-49 are how the sources print those sectors, so they are the codes."""
    rows = conn.execute(
        "SELECT code FROM naics_codes WHERE code LIKE '%-%' ORDER BY code"
    ).fetchall()
    assert rows == [("31-33",), ("44-45",), ("48-49",)]


def test_every_loaded_code_points_at_its_source(conn: sqlite3.Connection) -> None:
    assert conn.execute("SELECT count(*) FROM naics_codes WHERE source_id IS NULL").fetchone() == (
        0,
    )
    assert conn.execute("SELECT DISTINCT source_id FROM psc_codes").fetchall() == [
        ("gsa_psc_manual",)
    ]


def test_seed_is_recorded_on_its_sources_row_and_nowhere_else(conn: sqlite3.Connection) -> None:
    """A code list is release data, not a fetch: it stamps last_run_at and writes no
    ingestion_runs row, so the run log stays a log of work that went out to a source."""
    naics, psc = counts(conn)
    rows = conn.execute(
        "SELECT source_id, name, adapter_version, terms, last_run_at IS NOT NULL FROM sources"
        " WHERE source_id IN ('census_naics', 'gsa_psc_manual') ORDER BY source_id"
    ).fetchall()
    assert rows == [
        ("census_naics", "NAICS 2022, U.S. Census Bureau", "2022", TERMS, 1),
        ("gsa_psc_manual", "Product and Service Codes Manual, GSA", "2025-04", TERMS, 1),
    ]
    assert (naics, psc) > (0, 0)
    assert conn.execute("SELECT count(*) FROM ingestion_runs").fetchone() == (0,)


def test_seeding_again_moves_no_code(conn: sqlite3.Connection) -> None:
    before = counts(conn)
    titles = conn.execute("SELECT code, title FROM naics_codes ORDER BY code").fetchall()

    assert reference.seed(conn) == reference.SeedResult(naics=0, psc=0)

    assert counts(conn) == before
    assert conn.execute("SELECT code, title FROM naics_codes ORDER BY code").fetchall() == titles
    assert conn.execute("SELECT count(*) FROM ingestion_runs").fetchone() == (0,)


def test_seed_refreshes_a_code_whose_title_moved_on(conn: sqlite3.Connection) -> None:
    """What a new vintage looks like from the store's side: the row is corrected, not doubled,
    and the correction is attributed to its own run."""
    conn.execute("UPDATE naics_codes SET title = 'Stale' WHERE code = '541512'")
    before = counts(conn)

    assert reference.seed(conn) == reference.SeedResult(naics=1, psc=0)

    assert counts(conn) == before
    assert conn.execute("SELECT title FROM naics_codes WHERE code = '541512'").fetchone() == (
        "Computer Systems Design Services",
    )
    assert conn.execute("SELECT count(*) FROM ingestion_runs").fetchone() == (0,)


def test_a_hand_loaded_code_is_left_unclaimed(conn: sqlite3.Connection) -> None:
    """A code the release does not ship keeps its own provenance, which is none."""
    conn.execute("INSERT INTO naics_codes (code, title) VALUES ('999999', 'Local invention')")

    reference.seed(conn)

    assert conn.execute(
        "SELECT title, source_id FROM naics_codes WHERE code = '999999'"
    ).fetchone() == ("Local invention", None)


def test_v_notices_reads_the_titles(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO notices (notice_id, title, first_seen_at, last_seen_at, source_id, raw_json,"
        " naics_code, psc_code) VALUES ('n1', 'Help desk', ?, ?, 'sam_opportunities_api', '{}',"
        " '541512', 'DA01')",
        (NOW, NOW),
    )
    conn.execute(
        "INSERT INTO notices (notice_id, title, first_seen_at, last_seen_at, source_id, raw_json,"
        " naics_code, psc_code) VALUES ('n2', 'Retired codes', ?, ?, 'sam_opportunities_api',"
        " '{}', '517311', 'D399')",
        (NOW, NOW),
    )

    rows = conn.execute(
        "SELECT notice_id, naics_title, psc_title FROM v_notices ORDER BY notice_id"
    ).fetchall()
    assert rows == [
        (
            "n1",
            "Computer Systems Design Services",
            "IT AND TELECOM - BUSINESS APPLICATION/"
            "APPLICATION DEVELOPMENT SUPPORT SERVICES (LABOR)",
        ),
        ("n2", None, None),  # a 2017 code and a retired PSC still read, without a title
    ]


def test_a_store_short_of_the_migration_is_left_alone(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Staging a prefix of the migrations must still migrate: there is nowhere to put the
    lists until 0019, and the loader says so rather than failing."""
    staged = tmp_path / "migrations"
    staged.mkdir()
    for file in sorted(db.MIGRATIONS_DIR.glob("*.sql"))[:1]:
        shutil.copy(file, staged / file.name)
    monkeypatch.setattr(db, "MIGRATIONS_DIR", staged)
    conn = db.connect(db_path)

    assert db.migrate(conn) == ["0001_initial.sql"]

    assert counts(conn) == (0, 0)
    assert reference.loaded(conn) == ()
    conn.close()
