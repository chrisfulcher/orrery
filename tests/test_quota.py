import sqlite3

from mentor import quota, runs
from mentor.config import Settings


def insert_request(conn: sqlite3.Connection, run_id: int, requested_at: str | None = None) -> None:
    if requested_at is None:
        conn.execute(
            "INSERT INTO api_requests (run_id, endpoint) VALUES (?, 'https://api.sam.gov/x')",
            (run_id,),
        )
    else:
        conn.execute(
            "INSERT INTO api_requests (run_id, endpoint, requested_at)"
            " VALUES (?, 'https://api.sam.gov/x', ?)",
            (run_id, requested_at),
        )


def test_spent_today_counts_only_today(conn: sqlite3.Connection, run_id: int) -> None:
    insert_request(conn, run_id, "2000-01-01T00:00:00Z")
    insert_request(conn, run_id)
    assert quota.spent_today(conn) == 1


def test_remaining_clamps_at_zero(
    conn: sqlite3.Connection, run_id: int, settings: Settings
) -> None:
    small = settings.model_copy(update={"sam_daily_budget": 1})
    assert quota.remaining(conn, small) == 1
    insert_request(conn, run_id)
    insert_request(conn, run_id)
    assert quota.remaining(conn, small) == 0


def test_finish_derives_requests_spent(conn: sqlite3.Connection, run_id: int) -> None:
    other = runs.start(conn)
    insert_request(conn, run_id)
    insert_request(conn, run_id)
    insert_request(conn, other)

    runs.finish(conn, run_id, status="succeeded", records_returned=7)

    row = conn.execute(
        "SELECT status, records_returned, requests_spent, finished_at FROM ingestion_runs"
        " WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    assert row[:3] == ("succeeded", 7, 2)
    assert row[3] is not None
