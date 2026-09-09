"""SQLite connection and schema migrations.

Connections run in ``autocommit=True`` mode, so nothing opens a transaction
implicitly: ``conn.commit()`` and ``with conn:`` are no-ops, and code that
needs atomicity issues an explicit ``BEGIN`` / ``COMMIT``. This is what lets
``migrate`` run a whole SQL script inside one transaction and roll it back on
failure; in the legacy mode ``executescript`` would commit first.

Schema changes are numbered ``*.sql`` files in the ``migrations`` directory,
applied in filename order and recorded in ``schema_migrations``. A migration
file must not contain ``BEGIN`` or ``COMMIT``.
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).with_name("migrations")

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_TIMESTAMP_DEFAULT = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"


def utcnow() -> str:
    """Now in the stored timestamp format (UTC, second resolution)."""
    return datetime.now(UTC).strftime(TIMESTAMP_FORMAT)


class VecUnavailable(RuntimeError):
    """This Python's sqlite3 cannot load extensions; semantic search needs sqlite-vec."""


def load_vec(conn: sqlite3.Connection) -> None:
    """Load sqlite-vec into ``conn``. Call once per connection, and only where vector
    functions are used; everything else works without it."""
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            conn.enable_load_extension(False)
    except (ImportError, AttributeError, sqlite3.OperationalError) as exc:
        raise VecUnavailable(
            "semantic search needs the sqlite-vec extension, and this Python's sqlite3 cannot"
            " load extensions (built without --enable-loadable-sqlite-extensions)"
        ) from exc


@dataclass(frozen=True)
class Status:
    applied: list[str]
    pending: list[str]


def connect(path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the database at ``path`` with WAL and foreign keys on."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, autocommit=True)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def applied(conn: sqlite3.Connection) -> list[str]:
    """Names of applied migrations; empty if the database has never been migrated."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if exists is None:
        return []
    rows = conn.execute("SELECT name FROM schema_migrations ORDER BY name").fetchall()
    return [name for (name,) in rows]


def status(conn: sqlite3.Connection) -> Status:
    done = applied(conn)
    pending = [f.name for f in migration_files() if f.name not in done]
    return Status(applied=done, pending=pending)


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply every pending migration, each in its own transaction. Returns the names applied."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  name TEXT PRIMARY KEY,"
        f"  applied_at TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT})"
        ")"
    )
    done = set(applied(conn))
    applied_now: list[str] = []
    for file in migration_files():
        if file.name in done:
            continue
        conn.execute("BEGIN")
        try:
            conn.executescript(file.read_text())
            conn.execute("INSERT INTO schema_migrations (name) VALUES (?)", (file.name,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        applied_now.append(file.name)
    return applied_now
