"""The workspace layer: the user's documents, saved searches, pipeline, and company profile.

Private data: every row carries ``user_id`` and every query filters by it (DESIGN.md §3, §8).
v1 is single-user, so ``user_id`` defaults to the seeded ``local`` user. Workspace rows are
the user's own and may be updated or deleted, except that tracked opportunities are never
deleted (a dropped pursuit is ``no-bid``), their event log is append-only, and documents are
versioned: every save is a new version and old versions are never touched.
"""

import json
import sqlite3
from dataclasses import dataclass, replace
from enum import StrEnum

from mentor import db, documents, query
from mentor.documents import ProfileDocument, SearchDocument, WorkflowDocument

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


DOCUMENT_KINDS = ("profile", "workflow")


@dataclass(frozen=True)
class Document:
    """One version of a workspace document: the body as the user wrote it."""

    document_id: int
    kind: str
    version: int
    body: str
    created_at: str


def save_document(
    conn: sqlite3.Connection, kind: str, body: str, *, user_id: int = USER_ID
) -> Document:
    """Store ``body`` as the next version of the user's ``kind`` document."""
    if kind not in DOCUMENT_KINDS:
        raise ValueError(f"unknown document kind {kind!r}")
    conn.execute("BEGIN")
    try:
        document = _insert_document(conn, kind, body, user_id)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return document


def _insert_document(conn: sqlite3.Connection, kind: str, body: str, user_id: int) -> Document:
    """The next version, inside the caller's transaction."""
    (version,) = conn.execute(
        "SELECT coalesce(max(version), 0) + 1 FROM workspace_documents"
        " WHERE user_id = ? AND kind = ?",
        (user_id, kind),
    ).fetchone()
    row = conn.execute(
        "INSERT INTO workspace_documents (user_id, kind, version, body, created_at)"
        " VALUES (?, ?, ?, ?, ?) RETURNING document_id, kind, version, body, created_at",
        (user_id, kind, version, body, db.utcnow()),
    ).fetchone()
    return Document(*row)


def latest_document(
    conn: sqlite3.Connection, kind: str, *, user_id: int = USER_ID
) -> Document | None:
    row = conn.execute(
        "SELECT document_id, kind, version, body, created_at FROM workspace_documents"
        " WHERE user_id = ? AND kind = ? ORDER BY version DESC LIMIT 1",
        (user_id, kind),
    ).fetchone()
    return Document(*row) if row else None


def document_versions(
    conn: sqlite3.Connection, kind: str, *, user_id: int = USER_ID
) -> list[Document]:
    """Every version, oldest first."""
    rows = conn.execute(
        "SELECT document_id, kind, version, body, created_at FROM workspace_documents"
        " WHERE user_id = ? AND kind = ? ORDER BY version",
        (user_id, kind),
    ).fetchall()
    return [Document(*row) for row in rows]


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
    entity_id: int | None = None
    """The company's own contractor entity, once its UEI is in the graph (source 3)."""
    document: dict | None = None
    """The latest profile document, parsed; None until one has been saved."""


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


def workflow_document(conn: sqlite3.Connection, *, user_id: int = USER_ID) -> str:
    """The workflow as TOML: the latest saved document, else the default."""
    latest = latest_document(conn, "workflow", user_id=user_id)
    return latest.body if latest is not None else documents.DEFAULT_WORKFLOW


def workflow(conn: sqlite3.Connection, *, user_id: int = USER_ID) -> WorkflowDocument:
    return documents.parse(workflow_document(conn, user_id=user_id), WorkflowDocument)


def save_workflow(
    conn: sqlite3.Connection, body: str, *, user_id: int = USER_ID
) -> WorkflowDocument:
    """Validate ``body`` and store it as the next workflow version."""
    doc = documents.parse(body, WorkflowDocument)
    save_document(conn, "workflow", body, user_id=user_id)
    return doc


def search_document(saved: SavedSearch) -> str:
    """A saved search rendered for editing."""
    filters = saved.filters
    return documents.render_search(
        saved.name,
        SearchDocument(
            query=saved.query,
            naics=list(filters.naics or ()),
            set_asides=list(filters.set_asides or ()),
            agency_prefixes=list(filters.agency_prefixes or ()),
            deadline_within_days=filters.deadline_within_days,
        ),
    )


def save_search_document(
    conn: sqlite3.Connection, name: str, body: str, *, user_id: int = USER_ID
) -> SavedSearch:
    """Replace the named search with the contents of an edited document."""
    doc = documents.parse(body, SearchDocument)
    filters = query.Filters(
        naics=tuple(doc.naics) or None,
        set_asides=tuple(doc.set_asides) or None,
        agency_prefixes=tuple(doc.agency_prefixes) or None,
        deadline_within_days=doc.deadline_within_days,
    )
    return save_search(conn, name, query_text=doc.query, filters=filters, user_id=user_id)


def profile_document(conn: sqlite3.Connection, *, user_id: int = USER_ID) -> str:
    """The profile as TOML: the latest saved document, else one rendered from the legacy
    row, else the empty template."""
    latest = latest_document(conn, "profile", user_id=user_id)
    if latest is not None:
        return latest.body
    profile = get_profile(conn, user_id=user_id)
    if profile is None:
        return documents.render_profile(ProfileDocument())
    return documents.render_profile(
        ProfileDocument(
            company={"name": profile.name or "", "uei": profile.uei, "cage": profile.cage},
            offerings={
                "naics": list(profile.naics),
                "capability_statement": profile.capability_statement or "",
            },
            markets={"agency_prefixes": list(profile.target_agency_prefixes)},
            qualifications={"certifications": list(profile.certifications)},
        )
    )


def save_profile(conn: sqlite3.Connection, body: str, *, user_id: int = USER_ID) -> Profile:
    """Validate ``body``, store it as the next profile version, and project its typed fields
    into ``company_profiles`` so every existing read sees the same profile."""
    doc = documents.parse(body, ProfileDocument)
    conn.execute("BEGIN")
    try:
        _insert_document(conn, "profile", body, user_id)
        conn.execute(
            "INSERT INTO company_profiles (user_id, name, uei, cage, naics, certifications,"
            " capability_statement, target_agency_prefixes, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET name = excluded.name, uei = excluded.uei,"
            " cage = excluded.cage, naics = excluded.naics,"
            " certifications = excluded.certifications,"
            " capability_statement = excluded.capability_statement,"
            " target_agency_prefixes = excluded.target_agency_prefixes,"
            " updated_at = excluded.updated_at",
            (
                user_id,
                doc.company.name or None,
                doc.company.uei,
                doc.company.cage,
                _json_or_none(tuple(doc.offerings.naics)),
                _json_or_none(tuple(doc.qualifications.certifications)),
                doc.offerings.capability_statement or None,
                _json_or_none(tuple(doc.markets.agency_prefixes)),
                db.utcnow(),
            ),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
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
    entity = (
        conn.execute("SELECT entity_id FROM entities WHERE uei = ?", (row[1],)).fetchone()
        if row[1]
        else None
    )
    latest = latest_document(conn, "profile", user_id=user_id)
    document = None
    if latest is not None:
        try:
            document = documents.parse(latest.body, ProfileDocument).model_dump()
        except documents.DocumentError:  # a hand-written row from an older version
            document = None
    return Profile(
        row[0], row[1], row[2], _tuple(row[3]), _tuple(row[4]), row[5], _tuple(row[6]), row[7],
        entity[0] if entity else None, document,
    )  # fmt: skip


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
