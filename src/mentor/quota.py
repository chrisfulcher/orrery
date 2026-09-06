"""Local accounting for the SAM.gov daily request budget.

SAM.gov reports no quota headers, so the budget is enforced by counting our own keyed
requests in ``api_requests`` for the current UTC day.
"""

import sqlite3

from mentor.config import Settings


class BudgetExceeded(Exception):
    """Refusing to start a keyed request that would exceed today's budget."""


def spent_today(conn: sqlite3.Connection) -> int:
    (count,) = conn.execute(
        "SELECT count(*) FROM api_requests WHERE date(requested_at) = date('now')"
    ).fetchone()
    return count


def remaining(conn: sqlite3.Connection, settings: Settings) -> int:
    return max(settings.sam_daily_budget - spent_today(conn), 0)
