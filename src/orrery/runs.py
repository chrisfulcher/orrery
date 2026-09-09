"""Ingestion runs: the unit every keyed SAM.gov request is attributed to."""

import sqlite3
from datetime import date
from typing import Literal

from orrery import db


def start(
    conn: sqlite3.Connection,
    source_id: str = "sam_opportunities_api",
    *,
    posted_from: date | None = None,
    posted_to: date | None = None,
) -> int:
    """Open a run and return its id."""
    cursor = conn.execute(
        "INSERT INTO ingestion_runs (source_id, posted_from, posted_to, started_at)"
        " VALUES (?, ?, ?, ?)",
        (
            source_id,
            posted_from.isoformat() if posted_from else None,
            posted_to.isoformat() if posted_to else None,
            db.utcnow(),
        ),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def finish(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: Literal["succeeded", "failed"],
    records_returned: int | None = None,
    error: str | None = None,
    filter_json: str | None = None,
) -> None:
    """Close a run. ``requests_spent`` is derived from ``api_requests``, never counted by hand."""
    conn.execute(
        "UPDATE ingestion_runs SET"
        " finished_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),"
        " status = ?, records_returned = ?, error = ?, filter_json = ?,"
        " requests_spent = (SELECT count(*) FROM api_requests WHERE run_id = ?)"
        " WHERE run_id = ?",
        (status, records_returned, error, filter_json, run_id, run_id),
    )
