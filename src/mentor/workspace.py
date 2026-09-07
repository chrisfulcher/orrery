"""The workspace layer: the user's saved searches, pipeline, and company profile.

Private data: every row carries ``user_id`` and every query filters by it (DESIGN.md §3, §8).
v1 is single-user, so ``user_id`` defaults to the seeded ``local`` user. Workspace rows are
the user's own and may be updated or deleted, except that tracked opportunities are never
deleted (a dropped pursuit is ``no-bid``) and their event log is append-only.
"""

import json
import sqlite3
from dataclasses import dataclass, replace
from enum import StrEnum

from mentor import db, query

USER_ID = 1  # v1 single-user: the seeded 'local' row
STAGES = ("watching", "pursuing", "bid", "no-bid", "submitted", "won", "lost")


class Stage(StrEnum):
    WATCHING = "watching"
    PURSUING = "pursuing"
    BID = "bid"
    NO_BID = "no-bid"
    SUBMITTED = "submitted"
    WON = "won"
    LOST = "lost"


class NotFound(LookupError):
    """No such notice, saved search, or tracked opportunity for this user."""


@dataclass(frozen=True)
class Tracked:
    tracked_id: int
    notice_id: str
    stage: str
    pwin: int | None
    notes: str | None
    created_at: str
    updated_at: str
    title: str
    agency: str | None
    response_deadline: str | None


@dataclass(frozen=True)
class Event:
    event_id: int
    changed_at: str
    field: str
    old_value: str | None
    new_value: str | None


@dataclass(frozen=True)
class SavedSearch:
    search_id: int
    name: str
    query: str | None
    filters: query.Filters
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Profile:
    name: str | None
    uei: str | None
    cage: str | None
    naics: tuple[str, ...]
    certifications: tuple[str, ...]
    capability_statement: str | None
    target_agency_prefixes: tuple[str, ...]
    updated_at: str


PIPELINE = """
SELECT t.tracked_id, t.notice_id, t.stage, t.pwin, t.notes, t.created_at, t.updated_at,
       n.title, e.name, n.response_deadline
FROM tracked_opportunities AS t
JOIN notices AS n USING (notice_id)
LEFT JOIN entities AS e ON e.entity_id = n.agency_entity_id
WHERE t.user_id = :user_id
"""


def track(
    conn: sqlite3.Connection,
    notice_id: str,
    *,
    stage: str | None = None,
    pwin: int | None = None,
    notes: str | None = None,
    user_id: int = USER_ID,
) -> Tracked:
    """Start tracking a notice (default stage ``watching``) or update it. ``None`` leaves a
    field unchanged. Every field that changes appends one event with its old and new
    values, all in one transaction."""
    if conn.execute("SELECT 1 FROM notices WHERE notice_id = ?", (notice_id,)).fetchone() is None:
        raise NotFound(f"no notice {notice_id}")
    now = db.utcnow()
    given = {"stage": stage, "pwin": pwin, "notes": notes}
    conn.execute("BEGIN")
    try:
        row = conn.execute(
            "SELECT tracked_id, stage, pwin, notes FROM tracked_opportunities"
            " WHERE user_id = ? AND notice_id = ?",
            (user_id, notice_id),
        ).fetchone()
        if row is None:
            given["stage"] = stage or Stage.WATCHING
            (tracked_id,) = conn.execute(
                "INSERT INTO tracked_opportunities (user_id, notice_id, stage, pwin, notes,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING tracked_id",
                (user_id, notice_id, given["stage"], pwin, notes, now, now),
            ).fetchone()
            changes = {field: (None, value) for field, value in given.items() if value is not None}
        else:
            tracked_id, *current = row
            old = dict(zip(("stage", "pwin", "notes"), current, strict=True))
            changes = {
                field: (old[field], value)
                for field, value in given.items()
                if value is not None and value != old[field]
            }
            if changes:
                assignments = ", ".join(f"{field} = ?" for field in changes)
                conn.execute(
                    f"UPDATE tracked_opportunities SET {assignments}, updated_at = ?"
                    " WHERE tracked_id = ?",
                    (*(value for _, value in changes.values()), now, tracked_id),
                )
        conn.executemany(
            "INSERT INTO tracked_opportunity_events (tracked_id, user_id, changed_at, field,"
            " old_value, new_value) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (tracked_id, user_id, now, field, _text(before), _text(after))
                for field, (before, after) in changes.items()
            ],
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    row = conn.execute(
        PIPELINE + " AND t.tracked_id = :tracked_id", {"user_id": user_id, "tracked_id": tracked_id}
    ).fetchone()
    return Tracked(*row)


def pipeline(conn: sqlite3.Connection, *, user_id: int = USER_ID) -> list[Tracked]:
    """Every tracked opportunity, ordered by stage then soonest deadline."""
    rows = [Tracked(*row) for row in conn.execute(PIPELINE, {"user_id": user_id}).fetchall()]
    return sorted(
        rows,
        key=lambda t: (
            STAGES.index(t.stage),
            t.response_deadline is None,
            t.response_deadline or "",
        ),
    )


def history(conn: sqlite3.Connection, notice_id: str, *, user_id: int = USER_ID) -> list[Event]:
    """The change log of one tracked opportunity, oldest first."""
    rows = conn.execute(
        "SELECT ev.event_id, ev.changed_at, ev.field, ev.old_value, ev.new_value"
        " FROM tracked_opportunity_events AS ev"
        " JOIN tracked_opportunities AS t USING (tracked_id)"
        " WHERE t.user_id = ? AND t.notice_id = ? ORDER BY ev.event_id",
        (user_id, notice_id),
    ).fetchall()
    if not rows:
        raise NotFound(f"{notice_id} is not tracked")
    return [Event(*row) for row in rows]


def save_search(
    conn: sqlite3.Connection,
    name: str,
    *,
    query_text: str | None = None,
    filters: query.Filters = query.NO_FILTERS,
    user_id: int = USER_ID,
) -> SavedSearch:
    """Create or replace the named search. Re-adding a name is how it is edited."""
    params = filters.params()
    now = db.utcnow()
    conn.execute(
        "INSERT INTO saved_searches (user_id, name, query, naics, set_asides,"
        " agency_path_prefixes, deadline_within_days, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(user_id, name) DO UPDATE SET query = excluded.query,"
        " naics = excluded.naics, set_asides = excluded.set_asides,"
        " agency_path_prefixes = excluded.agency_path_prefixes,"
        " deadline_within_days = excluded.deadline_within_days, updated_at = excluded.updated_at",
        (
            user_id,
            name,
            query_text,
            params["naics"],
            params["set_asides"],
            params["agency_path_prefixes"],
            params["deadline_within_days"],
            now,
            now,
        ),
    )
    return get_search(conn, name, user_id=user_id)


def list_searches(conn: sqlite3.Connection, *, user_id: int = USER_ID) -> list[SavedSearch]:
    rows = conn.execute(
        "SELECT search_id, name, query, naics, set_asides, agency_path_prefixes,"
        " deadline_within_days, created_at, updated_at FROM saved_searches"
        " WHERE user_id = ? ORDER BY name",
        (user_id,),
    ).fetchall()
    return [_saved_search(row) for row in rows]


def get_search(conn: sqlite3.Connection, name: str, *, user_id: int = USER_ID) -> SavedSearch:
    row = conn.execute(
        "SELECT search_id, name, query, naics, set_asides, agency_path_prefixes,"
        " deadline_within_days, created_at, updated_at FROM saved_searches"
        " WHERE user_id = ? AND name = ?",
        (user_id, name),
    ).fetchone()
    if row is None:
        raise NotFound(f"no saved search named {name!r}")
    return _saved_search(row)


def delete_search(conn: sqlite3.Connection, name: str, *, user_id: int = USER_ID) -> None:
    deleted = conn.execute(
        "DELETE FROM saved_searches WHERE user_id = ? AND name = ?", (user_id, name)
    ).rowcount
    if not deleted:
        raise NotFound(f"no saved search named {name!r}")


def run_search(
    conn: sqlite3.Connection, name: str, *, limit: int = 50, user_id: int = USER_ID
) -> list[query.SearchHit]:
    """Run a saved search over active notices: keyword search when it has text, else the
    deadline-ordered list of notices passing its filters."""
    saved = get_search(conn, name, user_id=user_id)
    filters = replace(saved.filters, active_only=True)
    if saved.query:
        return query.search(conn, saved.query, limit=limit, filters=filters)
    return query.list_notices(conn, filters, limit=limit)


def set_profile(
    conn: sqlite3.Connection,
    *,
    user_id: int = USER_ID,
    name: str | None = None,
    uei: str | None = None,
    cage: str | None = None,
    naics: tuple[str, ...] | None = None,
    certifications: tuple[str, ...] | None = None,
    capability_statement: str | None = None,
    target_agency_prefixes: tuple[str, ...] | None = None,
) -> Profile:
    """Create or update the profile; fields left ``None`` keep their current values."""
    conn.execute(
        "INSERT INTO company_profiles (user_id, name, uei, cage, naics, certifications,"
        " capability_statement, target_agency_prefixes, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(user_id) DO UPDATE SET"
        " name = coalesce(excluded.name, name), uei = coalesce(excluded.uei, uei),"
        " cage = coalesce(excluded.cage, cage), naics = coalesce(excluded.naics, naics),"
        " certifications = coalesce(excluded.certifications, certifications),"
        " capability_statement = coalesce(excluded.capability_statement, capability_statement),"
        " target_agency_prefixes ="
        " coalesce(excluded.target_agency_prefixes, target_agency_prefixes),"
        " updated_at = excluded.updated_at",
        (
            user_id,
            name,
            uei,
            cage,
            _json_or_none(naics),
            _json_or_none(certifications),
            capability_statement,
            _json_or_none(target_agency_prefixes),
            db.utcnow(),
        ),
    )
    profile = get_profile(conn, user_id=user_id)
    assert profile is not None
    return profile


def get_profile(conn: sqlite3.Connection, *, user_id: int = USER_ID) -> Profile | None:
    row = conn.execute(
        "SELECT name, uei, cage, naics, certifications, capability_statement,"
        " target_agency_prefixes, updated_at FROM company_profiles WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if row is None:
        return None
    return Profile(
        row[0], row[1], row[2], _tuple(row[3]), _tuple(row[4]), row[5], _tuple(row[6]), row[7]
    )


def _saved_search(row: tuple) -> SavedSearch:
    filters = query.Filters(
        naics=_tuple(row[3]) or None,
        set_asides=_tuple(row[4]) or None,
        agency_prefixes=_tuple(row[5]) or None,
        deadline_within_days=row[6],
    )
    return SavedSearch(row[0], row[1], row[2], filters, row[7], row[8])


def _tuple(value: str | None) -> tuple[str, ...]:
    return tuple(json.loads(value)) if value else ()


def _json_or_none(value: tuple[str, ...] | None) -> str | None:
    return json.dumps(list(value)) if value else None


def _text(value: object) -> str | None:
    return None if value is None else str(value)
