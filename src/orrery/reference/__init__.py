"""Loading the shipped NAICS and PSC code lists into the store.

The CSVs beside this module are release data, not user data: they change only when orrery
ships a new vintage. So the loader is an upsert rather than an ingest, it runs at the end of
every ``db.migrate`` (a first install and every ``orrery top`` start), and it touches no code
row at all when the store already holds what the release carries.

The rows still carry provenance. Each list has a ``sources`` row created by migration 0019,
every loaded code points at it through ``source_id``, and each seed stamps that row's
``last_run_at``. Deliberately no ``ingestion_runs`` row: a run there is a unit of work that
fetched something and may have spent quota, and one per source per app start would bury the
real runs in a log of no-ops.

The table names below are module constants, never anything a caller supplies, which is why
they can be interpolated into the statements.
"""

import csv
import sqlite3
from dataclasses import dataclass
from importlib import resources

from orrery import db


@dataclass(frozen=True)
class CodeList:
    """One shipped code list: where it comes from, and where it goes."""

    source_id: str
    table: str
    file: str


NAICS = CodeList("census_naics", "naics_codes", "naics_2022.csv")
PSC = CodeList("gsa_psc_manual", "psc_codes", "psc_2025_04.csv")
LISTS = (NAICS, PSC)

MIGRATION = "0019_reference.sql"
"""The migration that adds ``source_id`` and the two ``sources`` rows. Until it is applied
there is nowhere to put the data, which is what a partially staged store looks like."""


@dataclass(frozen=True)
class SeedResult:
    """Rows written by one ``seed`` call, per list. Zero and zero is the steady state: the
    store already holds this release's lists."""

    naics: int
    psc: int


@dataclass(frozen=True)
class Loaded:
    """What a store holds for one list, for ``orrery db status``."""

    source_id: str
    table: str
    name: str
    vintage: str
    rows: int
    last_run_at: str | None


def read(codes: CodeList) -> list[tuple[str, str]]:
    """The ``code, title`` pairs of one shipped CSV, header dropped."""
    text = resources.files(__package__).joinpath(codes.file).read_text(encoding="utf-8")
    rows = csv.reader(text.splitlines())
    next(rows, None)
    return [(code, title) for code, title in rows]


def seed(conn: sqlite3.Connection) -> SeedResult:
    """Bring both code lists up to what this release ships, and return how many rows moved."""
    return SeedResult(naics=_seed(conn, NAICS), psc=_seed(conn, PSC))


def _stamp(conn: sqlite3.Connection, codes: CodeList) -> None:
    conn.execute(
        "UPDATE sources SET last_run_at = ? WHERE source_id = ?", (db.utcnow(), codes.source_id)
    )


def _seed(conn: sqlite3.Connection, codes: CodeList) -> int:
    stored = {
        code: (title, source_id)
        for code, title, source_id in conn.execute(
            f"SELECT code, title, source_id FROM {codes.table}"
        )
    }
    changed = [
        (code, title) for code, title in read(codes) if stored.get(code) != (title, codes.source_id)
    ]
    if not changed:
        _stamp(conn, codes)
        return 0
    conn.execute("BEGIN")
    try:
        conn.executemany(
            f"INSERT INTO {codes.table} (code, title, source_id) VALUES (?, ?, ?)"
            " ON CONFLICT(code) DO UPDATE SET title = excluded.title,"
            " source_id = excluded.source_id",
            [(code, title, codes.source_id) for code, title in changed],
        )
        _stamp(conn, codes)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(changed)


def loaded(conn: sqlite3.Connection) -> tuple[Loaded, ...]:
    """Each shipped list with the name and vintage its ``sources`` row states and the number
    of codes the store holds. Empty on a store migrated no further than 0018."""
    found = []
    for codes in LISTS:
        row = conn.execute(
            "SELECT name, adapter_version, last_run_at FROM sources WHERE source_id = ?",
            (codes.source_id,),
        ).fetchone()
        if row is None:
            continue
        (count,) = conn.execute(f"SELECT count(*) FROM {codes.table}").fetchone()
        found.append(Loaded(codes.source_id, codes.table, row[0], row[1], count, row[2]))
    return tuple(found)
