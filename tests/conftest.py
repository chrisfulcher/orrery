import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from mentor import db


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "mentor.sqlite"


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    """A migrated connection to a fresh database file."""
    connection = db.connect(db_path)
    db.migrate(connection)
    yield connection
    connection.close()
