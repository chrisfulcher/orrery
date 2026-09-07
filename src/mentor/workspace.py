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
    note: str | None = None
    """The rationale of a decision, the title of a task, or the role of a notice."""


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


LEGACY_STAGES = {
    "watching": ("identify", None),
    "pursuing": ("qualify", None),
    "bid": ("proposal", None),
    "submitted": ("submitted", None),
    "won": (None, "won"),
    "lost": (None, "lost"),
    "no-bid": (None, "no-bid"),
}
"""What `track --stage` meant before pursuits: a workflow key, or an outcome."""

NOTICE_ROLES = {
    "Sources Sought": "sources-sought",
    "Presolicitation": "presolicitation",
    "Award Notice": "award",
    "Modification/Amendment/Cancel": "amendment",
    "Solicitation": "solicitation",
    "Combined Synopsis/Solicitation": "solicitation",
}

PURSUIT_COLUMNS = (
    "pursuit_id, title, summary, stage, pwin, notes, held_until, outcome, closed_at,"
    " office_entity_id, office, office_code, naics_code, incumbent_contract_id, incumbent,"
    " incumbent_pop_end, notices, open_tasks, next_due, next_response_deadline, last_event_at,"
    " created_at, updated_at"
)


@dataclass(frozen=True)
class Pursuit:
    """One row of ``v_pursuits``."""

    pursuit_id: int
    title: str
    summary: str | None
    stage: str
    pwin: int | None
    notes: str | None
    held_until: str | None
    outcome: str | None
    closed_at: str | None
    office_entity_id: int | None
    office: str | None
    office_code: str | None
    naics_code: str | None
    incumbent_contract_id: int | None
    incumbent: str | None
    incumbent_pop_end: str | None
    notices: int
    open_tasks: int
    next_due: str | None
    next_response_deadline: str | None
    last_event_at: str | None
    created_at: str
    updated_at: str

    @property
    def open(self) -> bool:
        return self.closed_at is None


@dataclass(frozen=True)
class Task:
    task_id: int
    pursuit_id: int
    stage: str
    title: str
    origin: str
    due: str | None
    done_at: str | None
    created_at: str


@dataclass(frozen=True)
class LinkedNotice:
    notice_id: str
    role: str
    linked_at: str
    title: str
    notice_type: str | None
    response_deadline: str | None
    active: bool


@dataclass(frozen=True)
class GovDate:
    """A government date that constrains a pursuit, read from where it lives."""

    date: str
    kind: str
    """'response' (a linked notice's deadline) or 'pop_end' (the incumbent's period ends)."""
    label: str
    pursuit_id: int
    pursuit_title: str
    notice_id: str | None = None
    contract_id: int | None = None


@dataclass(frozen=True)
class PursuitDetail:
    pursuit: Pursuit
    tasks: tuple[Task, ...]
    notices: tuple[LinkedNotice, ...]
    incumbent: query.ContractRef | None
    related_notices: tuple[query.SearchHit, ...]
    """Notices carrying the incumbent's solicitation number: the prior competition."""
    dates: tuple[GovDate, ...]
    events: tuple["Event", ...]
    gate: str | None
    """The gate the current stage feeds, or None for a stage with no decision."""
    gate_ready: bool
    """Open, not held, gated, and every task of the current stage done."""


@dataclass(frozen=True)
class WorkItem:
    due: str
    kind: str
    """'task' (the user's) or 'response' (a linked notice's deadline)."""
    what: str
    pursuit_id: int
    pursuit_title: str
    stage: str
    overdue: bool
    task_id: int | None = None
    notice_id: str | None = None


@dataclass(frozen=True)
class Attention:
    reason: str
    """'gate ready', 'hold due', or 'stalled'."""
    detail: str
    pursuit_id: int
    pursuit_title: str
    stage: str


@dataclass(frozen=True)
class Dashboard:
    work: tuple[WorkItem, ...]
    attention: tuple[Attention, ...]
    dates: tuple[GovDate, ...]
    by_stage: tuple[tuple[str, int], ...]
    """Open pursuits per workflow stage, in workflow order."""


def _pursuit_row(conn: sqlite3.Connection, pursuit_id: int, user_id: int) -> Pursuit:
    row = conn.execute(
        f"SELECT {PURSUIT_COLUMNS} FROM v_pursuits WHERE user_id = ? AND pursuit_id = ?",
        (user_id, pursuit_id),
    ).fetchone()
    if row is None:
        raise NotFound(f"no pursuit {pursuit_id}")
    return Pursuit(*row)


def _event(
    conn: sqlite3.Connection,
    pursuit_id: int,
    user_id: int,
    now: str,
    field: str,
    old: object,
    new: object,
    note: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO pursuit_events (pursuit_id, user_id, changed_at, field, old_value,"
        " new_value, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (pursuit_id, user_id, now, field, _text(old), _text(new), note),
    )


def _seed_tasks(
    conn: sqlite3.Connection,
    pursuit_id: int,
    stage: str,
    workflow_doc: WorkflowDocument,
    now: str,
    user_id: int,
) -> int:
    """The stage's template tasks, once: a stage re-entered keeps the tasks it already has."""
    try:
        titles = workflow_doc.tasks_for(stage)
    except KeyError:
        return 0
    (seeded,) = conn.execute(
        "SELECT count(*) FROM pursuit_tasks WHERE pursuit_id = ? AND stage = ?"
        " AND origin = 'template'",
        (pursuit_id, stage),
    ).fetchone()
    if seeded or not titles:
        return 0
    conn.executemany(
        "INSERT INTO pursuit_tasks (pursuit_id, user_id, stage, title, origin, created_at)"
        " VALUES (?, ?, ?, ?, 'template', ?)",
        [(pursuit_id, user_id, stage, title, now) for title in titles],
    )
    return len(titles)


def _office_code(path_code: str | None) -> str | None:
    return path_code.rsplit(".", 1)[-1] if path_code else None


def _transaction(conn: sqlite3.Connection):
    return _Transaction(conn)


class _Transaction:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self) -> None:
        self.conn.execute("BEGIN")

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")


def new_pursuit(
    conn: sqlite3.Connection,
    title: str,
    *,
    summary: str | None = None,
    office_entity_id: int | None = None,
    office_code: str | None = None,
    naics: str | None = None,
    notice_id: str | None = None,
    contract_id: int | None = None,
    user_id: int = USER_ID,
) -> Pursuit:
    """Open a pursuit in the workflow's first stage with that stage's tasks seeded. A notice
    or an incumbent contract given here fills the office and NAICS the user did not."""
    if not title.strip():
        raise ValueError("a pursuit needs a title")
    if notice_id is not None:
        notice = conn.execute(
            "SELECT agency_entity_id, full_parent_path_code, naics_code FROM notices"
            " WHERE notice_id = ?",
            (notice_id,),
        ).fetchone()
        if notice is None:
            raise NotFound(f"no notice {notice_id}")
        office_entity_id = office_entity_id or notice[0]
        office_code = office_code or _office_code(notice[1])
        naics = naics or notice[2]
    if contract_id is not None:
        award = conn.execute(
            "SELECT awarding_entity_id, awarding_office_code, naics_code FROM contracts"
            " WHERE contract_id = ?",
            (contract_id,),
        ).fetchone()
        if award is None:
            raise NotFound(f"no contract {contract_id}")
        office_entity_id = office_entity_id or award[0]
        office_code = office_code or award[1]
        naics = naics or award[2]
    workflow_doc = workflow(conn, user_id=user_id)
    stage = workflow_doc.first_key()
    now = db.utcnow()
    with _transaction(conn):
        (pursuit_id,) = conn.execute(
            "INSERT INTO pursuits (user_id, title, summary, office_entity_id, office_code,"
            " naics_code, incumbent_contract_id, stage, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING pursuit_id",
            (
                user_id,
                title.strip(),
                summary,
                office_entity_id,
                office_code,
                naics,
                contract_id,
                stage,
                now,
                now,
            ),  # fmt: skip
        ).fetchone()
        _event(conn, pursuit_id, user_id, now, "stage", None, stage, "created")
        _seed_tasks(conn, pursuit_id, stage, workflow_doc, now, user_id)
        if notice_id is not None:
            _link(conn, pursuit_id, notice_id, None, now, user_id)
    return _pursuit_row(conn, pursuit_id, user_id)


def pursuit(conn: sqlite3.Connection, pursuit_id: int, *, user_id: int = USER_ID) -> PursuitDetail:
    """Everything about one pursuit, for its screen."""
    row = _pursuit_row(conn, pursuit_id, user_id)
    tasks = tuple(
        Task(*item)
        for item in conn.execute(
            "SELECT task_id, pursuit_id, stage, title, origin, due, done_at, created_at"
            " FROM pursuit_tasks WHERE pursuit_id = ?"
            " ORDER BY done_at IS NOT NULL, due IS NULL, due, task_id",
            (pursuit_id,),
        ).fetchall()
    )
    notices = tuple(
        LinkedNotice(*item[:6], bool(item[6]))
        for item in conn.execute(
            "SELECT pn.notice_id, pn.role, pn.linked_at, n.title, n.notice_type,"
            " n.response_deadline, n.active FROM pursuit_notices AS pn"
            " JOIN notices AS n ON n.notice_id = pn.notice_id"
            " WHERE pn.pursuit_id = ? ORDER BY pn.linked_at, pn.notice_id",
            (pursuit_id,),
        ).fetchall()
    )
    incumbent = (
        query.contract(conn, row.incumbent_contract_id) if row.incumbent_contract_id else None
    )
    related = (
        tuple(query.notices_for_solicitation(conn, incumbent.solicitation_identifier))
        if incumbent and incumbent.solicitation_identifier
        else ()
    )
    events = tuple(
        Event(*item)
        for item in conn.execute(
            "SELECT event_id, changed_at, field, old_value, new_value, note FROM pursuit_events"
            " WHERE pursuit_id = ? ORDER BY event_id",
            (pursuit_id,),
        ).fetchall()
    )
    workflow_doc = workflow(conn, user_id=user_id)
    try:
        gate_name = workflow_doc.stage(row.stage).gate
    except KeyError:
        gate_name = None
    open_here = any(t.done_at is None and t.stage == row.stage for t in tasks)
    gate_ready = bool(gate_name) and row.open and row.held_until is None and not open_here
    return PursuitDetail(
        row, tasks, notices, incumbent, related, tuple(_dates(row, notices, incumbent)),
        events, gate_name, gate_ready,
    )  # fmt: skip


def _dates(
    row: Pursuit, notices: tuple[LinkedNotice, ...], incumbent: query.ContractRef | None
) -> list[GovDate]:
    dates = [
        GovDate(n.response_deadline, "response", n.title, row.pursuit_id, row.title, n.notice_id)
        for n in notices
        if n.response_deadline
    ]
    if incumbent and incumbent.pop_end:
        dates.append(
            GovDate(
                incumbent.pop_end,
                "pop_end",
                f"{incumbent.vendor or '-'} {incumbent.piid} ends",
                row.pursuit_id,
                row.title,
                contract_id=incumbent.contract_id,
            )  # fmt: skip
        )
    return sorted(dates, key=lambda d: d.date)


def pursuits(
    conn: sqlite3.Connection,
    *,
    stage: str | None = None,
    include_closed: bool = False,
    user_id: int = USER_ID,
) -> list[Pursuit]:
    """Pursuits in workflow order, then soonest task due, then title."""
    rows = [
        Pursuit(*row)
        for row in conn.execute(
            f"SELECT {PURSUIT_COLUMNS} FROM v_pursuits WHERE user_id = :user_id"
            " AND (:stage IS NULL OR stage = :stage)"
            " AND (:include_closed OR closed_at IS NULL)",
            {"user_id": user_id, "stage": stage, "include_closed": int(include_closed)},
        ).fetchall()
    ]
    order = {key: i for i, key in enumerate(workflow(conn, user_id=user_id).keys())}
    return sorted(
        rows,
        key=lambda p: (
            order.get(p.stage, len(order)),
            p.next_due is None,
            p.next_due or "",
            p.title.lower(),
        ),
    )


def pursuit_for_notice(
    conn: sqlite3.Connection, notice_id: str, *, user_id: int = USER_ID
) -> Pursuit | None:
    """The open pursuit this notice belongs to, else the latest closed one, else None."""
    row = conn.execute(
        "SELECT p.pursuit_id FROM pursuit_notices AS pn JOIN pursuits AS p USING (pursuit_id)"
        " WHERE pn.user_id = ? AND pn.notice_id = ?"
        " ORDER BY p.closed_at IS NOT NULL, p.pursuit_id DESC LIMIT 1",
        (user_id, notice_id),
    ).fetchone()
    return _pursuit_row(conn, row[0], user_id) if row else None


def _link(
    conn: sqlite3.Connection,
    pursuit_id: int,
    notice_id: str,
    role: str | None,
    now: str,
    user_id: int,
) -> str:
    notice = conn.execute(
        "SELECT notice_type FROM notices WHERE notice_id = ?", (notice_id,)
    ).fetchone()
    if notice is None:
        raise NotFound(f"no notice {notice_id}")
    role = role or NOTICE_ROLES.get(notice[0] or "", "other")
    inserted = conn.execute(
        "INSERT OR IGNORE INTO pursuit_notices (pursuit_id, user_id, notice_id, role, linked_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (pursuit_id, user_id, notice_id, role, now),
    ).rowcount
    if inserted:
        _event(conn, pursuit_id, user_id, now, "notice", None, notice_id, role)
    return role


def link_notice(
    conn: sqlite3.Connection,
    pursuit_id: int,
    notice_id: str,
    *,
    role: str | None = None,
    user_id: int = USER_ID,
) -> LinkedNotice:
    """Attach a notice to a pursuit; the role defaults from the notice type. Idempotent."""
    _pursuit_row(conn, pursuit_id, user_id)
    now = db.utcnow()
    with _transaction(conn):
        _link(conn, pursuit_id, notice_id, role, now, user_id)
        conn.execute("UPDATE pursuits SET updated_at = ? WHERE pursuit_id = ?", (now, pursuit_id))
    return next(
        n for n in pursuit(conn, pursuit_id, user_id=user_id).notices if n.notice_id == notice_id
    )


def gate(
    conn: sqlite3.Connection,
    pursuit_id: int,
    decision: str,
    why: str,
    *,
    until: str | None = None,
    user_id: int = USER_ID,
) -> Pursuit:
    """Record the decision at the current stage's gate: ``go`` advances and seeds the next
    stage's tasks, ``no-go`` closes the pursuit as no-bid, ``hold`` parks it until a date."""
    row = _pursuit_row(conn, pursuit_id, user_id)
    if not why.strip():
        raise ValueError("a gate decision needs a rationale (--why)")
    if not row.open:
        raise ValueError(f"pursuit {pursuit_id} is closed; reopen it first")
    workflow_doc = workflow(conn, user_id=user_id)
    try:
        spec = workflow_doc.stage(row.stage)
    except KeyError:
        raise ValueError(f"stage {row.stage!r} is not in the workflow document") from None
    if not spec.gate:
        raise ValueError(f"stage {row.stage} has no gate")
    now = db.utcnow()
    with _transaction(conn):
        if decision == "go":
            nxt = workflow_doc.next_key(row.stage)
            if nxt is None:
                raise ValueError(f"{row.stage} is the last stage")
            _event(conn, pursuit_id, user_id, now, "gate", spec.gate, "go", why)
            _event(conn, pursuit_id, user_id, now, "stage", row.stage, nxt, spec.gate)
            conn.execute(
                "UPDATE pursuits SET stage = ?, held_until = NULL, updated_at = ?"
                " WHERE pursuit_id = ?",
                (nxt, now, pursuit_id),
            )
            _seed_tasks(conn, pursuit_id, nxt, workflow_doc, now, user_id)
        elif decision == "no-go":
            _event(conn, pursuit_id, user_id, now, "gate", spec.gate, "no-go", why)
            _event(conn, pursuit_id, user_id, now, "outcome", row.outcome, "no-bid", why)
            _event(conn, pursuit_id, user_id, now, "closed", None, now, spec.gate)
            conn.execute(
                "UPDATE pursuits SET outcome = 'no-bid', closed_at = ?, held_until = NULL,"
                " updated_at = ? WHERE pursuit_id = ?",
                (now, now, pursuit_id),
            )
        elif decision == "hold":
            if not until:
                raise ValueError("a hold needs a revisit date (--until YYYY-MM-DD)")
            _event(conn, pursuit_id, user_id, now, "gate", spec.gate, "hold", why)
            _event(conn, pursuit_id, user_id, now, "hold", row.held_until, until, why)
            conn.execute(
                "UPDATE pursuits SET held_until = ?, updated_at = ? WHERE pursuit_id = ?",
                (until, now, pursuit_id),
            )
        else:
            raise ValueError(f"decision must be go, no-go, or hold, not {decision!r}")
    return _pursuit_row(conn, pursuit_id, user_id)


def move_back(
    conn: sqlite3.Connection, pursuit_id: int, stage: str, why: str, *, user_id: int = USER_ID
) -> Pursuit:
    """Return an open pursuit to an earlier stage, with a reason. Its tasks stay."""
    row = _pursuit_row(conn, pursuit_id, user_id)
    if not why.strip():
        raise ValueError("moving back needs a rationale (--why)")
    if not row.open:
        raise ValueError(f"pursuit {pursuit_id} is closed; reopen it first")
    workflow_doc = workflow(conn, user_id=user_id)
    earlier = workflow_doc.previous_keys(row.stage) if row.stage in workflow_doc.keys() else []
    if stage not in earlier:
        raise ValueError(f"{stage!r} is not an earlier stage than {row.stage}")
    now = db.utcnow()
    with _transaction(conn):
        _event(conn, pursuit_id, user_id, now, "stage", row.stage, stage, why)
        conn.execute(
            "UPDATE pursuits SET stage = ?, held_until = NULL, updated_at = ? WHERE pursuit_id = ?",
            (stage, now, pursuit_id),
        )
        _seed_tasks(conn, pursuit_id, stage, workflow_doc, now, user_id)
    return _pursuit_row(conn, pursuit_id, user_id)


def reopen(
    conn: sqlite3.Connection, pursuit_id: int, why: str, *, user_id: int = USER_ID
) -> Pursuit:
    """Reopen a closed pursuit at the stage it was in; its outcome is cleared."""
    row = _pursuit_row(conn, pursuit_id, user_id)
    if not why.strip():
        raise ValueError("reopening needs a rationale (--why)")
    if row.open:
        raise ValueError(f"pursuit {pursuit_id} is open")
    now = db.utcnow()
    with _transaction(conn):
        _event(conn, pursuit_id, user_id, now, "reopened", row.outcome, None, why)
        conn.execute(
            "UPDATE pursuits SET outcome = NULL, closed_at = NULL, updated_at = ?"
            " WHERE pursuit_id = ?",
            (now, pursuit_id),
        )
    return _pursuit_row(conn, pursuit_id, user_id)


def add_task(
    conn: sqlite3.Connection,
    pursuit_id: int,
    title: str,
    *,
    due: str | None = None,
    stage: str | None = None,
    user_id: int = USER_ID,
) -> Task:
    row = _pursuit_row(conn, pursuit_id, user_id)
    if not title.strip():
        raise ValueError("a task needs a title")
    now = db.utcnow()
    with _transaction(conn):
        item = conn.execute(
            "INSERT INTO pursuit_tasks (pursuit_id, user_id, stage, title, origin, due,"
            " created_at) VALUES (?, ?, ?, ?, 'user', ?, ?)"
            " RETURNING task_id, pursuit_id, stage, title, origin, due, done_at, created_at",
            (pursuit_id, user_id, stage or row.stage, title.strip(), due, now),
        ).fetchone()
        _event(conn, pursuit_id, user_id, now, "task", None, title.strip(), "added")
        conn.execute("UPDATE pursuits SET updated_at = ? WHERE pursuit_id = ?", (now, pursuit_id))
    return Task(*item)


def complete_task(conn: sqlite3.Connection, task_id: int, *, user_id: int = USER_ID) -> Task:
    row = conn.execute(
        "SELECT pursuit_id, title, done_at FROM pursuit_tasks WHERE task_id = ? AND user_id = ?",
        (task_id, user_id),
    ).fetchone()
    if row is None:
        raise NotFound(f"no task {task_id}")
    now = db.utcnow()
    if row[2] is None:
        with _transaction(conn):
            conn.execute("UPDATE pursuit_tasks SET done_at = ? WHERE task_id = ?", (now, task_id))
            _event(conn, row[0], user_id, now, "task", None, row[1], "done")
            conn.execute("UPDATE pursuits SET updated_at = ? WHERE pursuit_id = ?", (now, row[0]))
    item = conn.execute(
        "SELECT task_id, pursuit_id, stage, title, origin, due, done_at, created_at"
        " FROM pursuit_tasks WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    return Task(*item)


def update_pursuit(
    conn: sqlite3.Connection,
    pursuit_id: int,
    *,
    title: str | None = None,
    summary: str | None = None,
    pwin: int | None = None,
    notes: str | None = None,
    incumbent_contract_id: int | None = None,
    user_id: int = USER_ID,
) -> Pursuit:
    """Change the descriptive fields; PWin and notes changes are events."""
    row = _pursuit_row(conn, pursuit_id, user_id)
    if incumbent_contract_id is not None and query.contract(conn, incumbent_contract_id) is None:
        raise NotFound(f"no contract {incumbent_contract_id}")
    now = db.utcnow()
    with _transaction(conn):
        if pwin is not None and pwin != row.pwin:
            _event(conn, pursuit_id, user_id, now, "pwin", row.pwin, pwin)
        if notes is not None and notes != row.notes:
            _event(conn, pursuit_id, user_id, now, "notes", row.notes, notes)
        conn.execute(
            "UPDATE pursuits SET title = coalesce(?, title), summary = coalesce(?, summary),"
            " pwin = coalesce(?, pwin), notes = coalesce(?, notes),"
            " incumbent_contract_id = coalesce(?, incumbent_contract_id), updated_at = ?"
            " WHERE pursuit_id = ?",
            (title, summary, pwin, notes, incumbent_contract_id, now, pursuit_id),
        )
    return _pursuit_row(conn, pursuit_id, user_id)


def set_outcome(
    conn: sqlite3.Connection, pursuit_id: int, outcome: str, why: str, *, user_id: int = USER_ID
) -> Pursuit:
    """``won`` moves the pursuit to the last stage and keeps it open for post-award work;
    ``lost`` and ``no-bid`` close it."""
    row = _pursuit_row(conn, pursuit_id, user_id)
    if outcome not in ("won", "lost", "no-bid"):
        raise ValueError(f"outcome must be won, lost, or no-bid, not {outcome!r}")
    if not why.strip():
        raise ValueError("an outcome needs a rationale (--why)")
    if not row.open:
        raise ValueError(f"pursuit {pursuit_id} is closed; reopen it first")
    workflow_doc = workflow(conn, user_id=user_id)
    now = db.utcnow()
    with _transaction(conn):
        _event(conn, pursuit_id, user_id, now, "outcome", row.outcome, outcome, why)
        if outcome == "won":
            last = workflow_doc.last_key()
            if row.stage != last:
                _event(conn, pursuit_id, user_id, now, "stage", row.stage, last, "won")
            conn.execute(
                "UPDATE pursuits SET outcome = 'won', stage = ?, held_until = NULL, updated_at = ?"
                " WHERE pursuit_id = ?",
                (last, now, pursuit_id),
            )
            _seed_tasks(conn, pursuit_id, last, workflow_doc, now, user_id)
        else:
            _event(conn, pursuit_id, user_id, now, "closed", None, now, outcome)
            conn.execute(
                "UPDATE pursuits SET outcome = ?, closed_at = ?, held_until = NULL, updated_at = ?"
                " WHERE pursuit_id = ?",
                (outcome, now, now, pursuit_id),
            )
    return _pursuit_row(conn, pursuit_id, user_id)


def close_pursuit(conn: sqlite3.Connection, pursuit_id: int, *, user_id: int = USER_ID) -> Pursuit:
    """Take a pursuit off the board (post-award work done). Never deleted."""
    row = _pursuit_row(conn, pursuit_id, user_id)
    if row.open:
        now = db.utcnow()
        with _transaction(conn):
            _event(conn, pursuit_id, user_id, now, "closed", None, now, row.outcome)
            conn.execute(
                "UPDATE pursuits SET closed_at = ?, updated_at = ? WHERE pursuit_id = ?",
                (now, now, pursuit_id),
            )
    return _pursuit_row(conn, pursuit_id, user_id)


def dashboard(
    conn: sqlite3.Connection,
    *,
    days: int = 7,
    stall_days: int = 14,
    horizon_days: int = 60,
    user_id: int = USER_ID,
) -> Dashboard:
    """This week's work on the shop's own calendar, what needs a decision or a look, and
    the government's dates as a secondary strip."""
    today = db.utcnow()[:10]
    soon = _plus_days(today, days)
    horizon = _plus_days(today, horizon_days)
    workflow_doc = workflow(conn, user_id=user_id)
    gated = set(workflow_doc.gated_keys())
    open_rows = pursuits(conn, user_id=user_id)
    by_id = {p.pursuit_id: p for p in open_rows}

    work: list[WorkItem] = []
    for item in conn.execute(
        "SELECT t.task_id, t.pursuit_id, t.title, t.due FROM pursuit_tasks AS t"
        " JOIN pursuits AS p USING (pursuit_id) WHERE t.user_id = ? AND t.done_at IS NULL"
        " AND t.due IS NOT NULL AND t.due <= ? AND p.closed_at IS NULL ORDER BY t.due, t.task_id",
        (user_id, soon),
    ).fetchall():
        p = by_id[item[1]]
        work.append(
            WorkItem(
                item[3], "task", item[2], p.pursuit_id, p.title, p.stage, item[3] < today, item[0]
            )
        )
    for item in conn.execute(
        "SELECT pn.pursuit_id, pn.notice_id, n.title, substr(n.response_deadline, 1, 10)"
        " FROM pursuit_notices AS pn JOIN notices AS n ON n.notice_id = pn.notice_id"
        " JOIN pursuits AS p ON p.pursuit_id = pn.pursuit_id WHERE pn.user_id = ?"
        " AND p.closed_at IS NULL AND n.response_deadline IS NOT NULL"
        " AND substr(n.response_deadline, 1, 10) BETWEEN ? AND ?",
        (user_id, today, soon),
    ).fetchall():
        p = by_id[item[0]]
        if p.stage in gated:
            work.append(
                WorkItem(
                    item[3],
                    "response",
                    f"response due: {item[2]}",
                    p.pursuit_id,
                    p.title,
                    p.stage,
                    False,
                    notice_id=item[1],
                )  # fmt: skip
            )
    work.sort(key=lambda w: (w.due, w.kind != "response", w.what))

    attention: list[Attention] = []
    open_by_stage = {
        (pid, stage): n
        for pid, stage, n in conn.execute(
            "SELECT pursuit_id, stage, count(*) FROM pursuit_tasks WHERE user_id = ?"
            " AND done_at IS NULL GROUP BY pursuit_id, stage",
            (user_id,),
        ).fetchall()
    }
    stale_before = _plus_days(today, -stall_days)
    for p in open_rows:
        if p.held_until is not None:
            if p.held_until <= soon:
                attention.append(
                    Attention("hold due", f"revisit {p.held_until}", p.pursuit_id, p.title, p.stage)
                )
            continue
        if p.stage in gated and not open_by_stage.get((p.pursuit_id, p.stage)):
            attention.append(
                Attention(
                    "gate ready",
                    f"{workflow_doc.stage(p.stage).gate}: every task done",
                    p.pursuit_id,
                    p.title,
                    p.stage,
                )  # fmt: skip
            )
        elif p.stage in gated and (p.last_event_at or p.created_at)[:10] < stale_before:
            days_quiet = _days_between((p.last_event_at or p.created_at)[:10], today)
            attention.append(
                Attention(
                    "stalled", f"no activity for {days_quiet} days", p.pursuit_id, p.title, p.stage
                )
            )

    priority = {"gate ready": 0, "hold due": 1, "stalled": 2}
    attention.sort(key=lambda a: (priority[a.reason], a.pursuit_title.lower()))

    dates: list[GovDate] = []
    for p in open_rows:
        detail_dates = _dates(
            p,
            tuple(
                LinkedNotice(*item[:6], bool(item[6]))
                for item in conn.execute(
                    "SELECT pn.notice_id, pn.role, pn.linked_at, n.title, n.notice_type,"
                    " n.response_deadline, n.active FROM pursuit_notices AS pn"
                    " JOIN notices AS n ON n.notice_id = pn.notice_id WHERE pn.pursuit_id = ?",
                    (p.pursuit_id,),
                ).fetchall()
            ),
            query.contract(conn, p.incumbent_contract_id) if p.incumbent_contract_id else None,
        )
        dates += [d for d in detail_dates if today <= d.date[:10] <= horizon]
    dates.sort(key=lambda d: d.date)

    counts = {key: 0 for key in workflow_doc.keys()}
    for p in open_rows:
        counts[p.stage] = counts.get(p.stage, 0) + 1
    return Dashboard(tuple(work), tuple(attention), tuple(dates), tuple(counts.items()))


def _plus_days(day: str, days: int) -> str:
    from datetime import date, timedelta

    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def _days_between(earlier: str, later: str) -> int:
    from datetime import date

    return (date.fromisoformat(later) - date.fromisoformat(earlier)).days


# The commands that predate pursuits, kept working over them.


def _tracked(conn: sqlite3.Connection, p: Pursuit, notice_id: str) -> Tracked:
    row = conn.execute(
        "SELECT n.title, e.name, n.response_deadline FROM notices AS n"
        " LEFT JOIN entities AS e ON e.entity_id = n.agency_entity_id WHERE n.notice_id = ?",
        (notice_id,),
    ).fetchone()
    return Tracked(
        p.pursuit_id, notice_id, p.stage, p.pwin, p.notes, p.created_at, p.updated_at, *row
    )


def track(
    conn: sqlite3.Connection,
    notice_id: str,
    *,
    stage: str | None = None,
    pwin: int | None = None,
    notes: str | None = None,
    user_id: int = USER_ID,
) -> Tracked:
    """The pre-pursuit command: start (or update) the pursuit this notice belongs to. A stage
    given by its old name or a workflow key advances through recorded gate decisions; an
    outcome name sets the outcome; moving backwards is refused (use ``move_back``)."""
    p = pursuit_for_notice(conn, notice_id, user_id=user_id)
    if p is None:
        title = conn.execute(
            "SELECT title FROM notices WHERE notice_id = ?", (notice_id,)
        ).fetchone()
        if title is None:
            raise NotFound(f"no notice {notice_id}")
        p = new_pursuit(conn, title[0], notice_id=notice_id, user_id=user_id)
    if stage is not None:
        key, outcome = LEGACY_STAGES.get(stage, (stage, None))
        if outcome is not None:
            if p.outcome != outcome:
                p = set_outcome(conn, p.pursuit_id, outcome, "set through track", user_id=user_id)
        elif key != p.stage:
            keys = workflow(conn, user_id=user_id).keys()
            if key not in keys:
                raise ValueError(f"{key!r} is not a stage of the workflow")
            if keys.index(key) < keys.index(p.stage):
                raise ValueError(f"{key} is earlier than {p.stage}; use `mentor pursuit back`")
            while p.stage != key:
                p = gate(conn, p.pursuit_id, "go", "set through track", user_id=user_id)
    if pwin is not None or notes is not None:
        p = update_pursuit(conn, p.pursuit_id, pwin=pwin, notes=notes, user_id=user_id)
    return _tracked(conn, p, notice_id)


def pipeline(conn: sqlite3.Connection, *, user_id: int = USER_ID) -> list[Tracked]:
    """One row per pursuit and linked notice, closed ones included, in workflow order then
    soonest deadline: what ``mentor pipeline`` has always shown."""
    rows = []
    for p in pursuits(conn, include_closed=True, user_id=user_id):
        for (notice_id,) in conn.execute(
            "SELECT notice_id FROM pursuit_notices WHERE pursuit_id = ? ORDER BY linked_at",
            (p.pursuit_id,),
        ).fetchall():
            rows.append(_tracked(conn, p, notice_id))
    return sorted(
        rows,
        key=lambda t: (
            [p.pursuit_id for p in pursuits(conn, include_closed=True, user_id=user_id)].index(
                t.tracked_id
            ),
            t.response_deadline is None,
            t.response_deadline or "",
        ),
    )


def history(conn: sqlite3.Connection, notice_id: str, *, user_id: int = USER_ID) -> list[Event]:
    """The event log of the pursuit this notice belongs to, oldest first."""
    p = pursuit_for_notice(conn, notice_id, user_id=user_id)
    if p is None:
        raise NotFound(f"{notice_id} is not tracked")
    return list(pursuit(conn, p.pursuit_id, user_id=user_id).events)


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
    in_use = {
        stage
        for (stage,) in conn.execute(
            "SELECT DISTINCT stage FROM pursuits WHERE user_id = ? AND closed_at IS NULL",
            (user_id,),
        ).fetchall()
    }
    missing = sorted(in_use - set(doc.keys()))
    if missing:
        raise ValueError(f"stage keys in use by open pursuits cannot be removed: {missing}")
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
