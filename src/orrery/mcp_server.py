"""MCP server: the store for AI agents, over stdio. Interface version 1 (DESIGN.md §4).

Tools are thin wrappers over the query and workspace modules and return the engine's own
records. Reads plus the workspace writes (pursuits, track, save_search). Nothing here spends
SAM.gov quota or contacts the network: ingest, fetch, and embed are deliberately not tools.
Tools and their fields are only ever added.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime

from mcp.server.mcpserver import MCPServer

from orrery import __version__, db, query, quota, workspace
from orrery.config import Settings
from orrery.fetch import queue

INTERFACE_VERSION = 1

server = MCPServer(
    "orrery",
    version=__version__,
    instructions=(
        f"orrery MCP interface v{INTERFACE_VERSION}: a local store of U.S. federal contract"
        " opportunities (SAM.gov notices, their documents, agencies, contractors, USAspending"
        " awards, and the user's pipeline)."
        " Notice ids are 32-character hex SAM.gov ids. Tools and fields are only ever added."
    ),
)


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    with closing(db.connect(Settings().db_path)) as conn:
        yield conn


def _tuple(values: list[str] | None) -> tuple[str, ...] | None:
    return tuple(values) if values else None


@server.tool()
def search(
    text: str,
    limit: int = 20,
    naics: list[str] | None = None,
    set_aside: list[str] | None = None,
    agency: list[str] | None = None,
    deadline_days: int | None = None,
) -> list[query.SearchHit]:
    """Keyword search across notice text and attachment text (FTS5 syntax allowed); one row
    per solicitation, headed by its furthest-along notice, with the best hit found anywhere in
    the group, its source and snippet, the group's stage (`notice_type`) and size (`notices`),
    and the stored summary, work type, and stated set-aside when `orrery summarize` has run.
    Filters: NAICS codes, set-aside codes, agency path-code prefixes, and a deadline window in
    days."""
    filters = query.Filters(
        naics=_tuple(naics),
        set_asides=_tuple(set_aside),
        agency_prefixes=_tuple(agency),
        deadline_within_days=deadline_days,
    )
    with _conn() as conn:
        return query.search(conn, text, limit=limit, filters=filters)


@server.tool()
def notice(notice_id: str) -> query.NoticeDetail:
    """Everything public about one notice: typed fields, description, agency chain,
    attachments with fetch and extraction status, version count, the SAM.gov page URL, the
    incumbent award, recent awards from the same office, the government contacts, and the
    stored summary with its work type, keywords, stated set-aside, and model."""
    with _conn() as conn:
        detail = query.notice(conn, notice_id)
    if detail is None:
        raise workspace.NotFound(f"no notice {notice_id}")
    return detail


@server.tool()
def entity(entity_id: int) -> query.EntityDetail:
    """One agency, office, or contractor: its place in the hierarchy, children, aliases,
    notice count, recent notices, awards made or won, and sourced facts."""
    with _conn() as conn:
        detail = query.entity(conn, entity_id)
    if detail is None:
        raise workspace.NotFound(f"no entity {entity_id}")
    return detail


@server.tool()
def awards(
    office: str | None = None,
    uei: str | None = None,
    naics: str | None = None,
    solicitation: str | None = None,
    limit: int = 20,
) -> list[query.ContractRef]:
    """Award history from USAspending, newest action first. Filters: awarding office code
    (the last segment of an agency path), vendor UEI, NAICS code, solicitation identifier."""
    with _conn() as conn:
        return query.awards(
            conn, office_code=office, uei=uei, naics=naics, solicitation=solicitation, limit=limit
        )


@server.tool()
def assessments(pursuit_id: int) -> list[workspace.AssessmentRecord]:
    """Stored AI assessments of a pursuit, newest first, each with its provenance (slot,
    provider, model, prompt and profile versions, tokens). Running one is a CLI command:
    this server never contacts a model."""
    with _conn() as conn:
        return workspace.assessments(conn, pursuit_id)


@server.tool()
def recompetes(
    months: int = 18,
    naics: list[str] | None = None,
    office: str | None = None,
    set_aside: str | None = None,
    limit: int = 50,
) -> list[query.ContractRef]:
    """The recompete radar: awards whose period of performance (options included) ends within
    the window, soonest first; the contract id is what new_pursuit takes as the incumbent."""
    with _conn() as conn:
        return query.recompetes(
            conn, months=months, naics=_tuple(naics), office_code=office, set_aside=set_aside,
            limit=limit,
        )  # fmt: skip


@server.tool()
def contractor(uei: str) -> query.EntityDetail:
    """One contractor by UEI: names seen, awards won with their offices and values, and every
    sourced fact about it."""
    with _conn() as conn:
        detail = query.contractor(conn, uei)
    if detail is None:
        raise workspace.NotFound(f"no contractor {uei}")
    return detail


@server.tool()
def upcoming(days: int = 7, limit: int = 50) -> list[query.SearchHit]:
    """Active solicitations with a response deadline within the next N days, soonest first.
    One row per solicitation, headed by its furthest-along notice; the deadline is the latest
    one still open in the group."""
    with _conn() as conn:
        return query.upcoming(conn, days=days, limit=limit)


@server.tool()
def pipeline() -> list[workspace.Tracked]:
    """The user's tracked opportunities with stage and win probability, by stage then deadline."""
    with _conn() as conn:
        return workspace.pipeline(conn)


@server.tool()
def track(
    notice_id: str,
    stage: workspace.Stage | None = None,
    pwin: int | None = None,
    notes: str | None = None,
) -> workspace.Tracked:
    """Start tracking a notice (default stage watching) or update its stage, win probability
    (0 to 100), or notes. Every change is kept in the history. Nothing is ever untracked;
    a dropped pursuit is stage no-bid."""
    with _conn() as conn:
        return workspace.track(
            conn, notice_id, stage=stage.value if stage else None, pwin=pwin, notes=notes
        )


@server.tool()
def history(notice_id: str) -> list[workspace.Event]:
    """The change log of a tracked notice: every stage, win-probability, and notes change."""
    with _conn() as conn:
        return workspace.history(conn, notice_id)


@server.tool()
def pursuits(stage: str | None = None, include_closed: bool = False) -> list[workspace.Pursuit]:
    """The user's pursuits by workflow stage (open ones unless include_closed): title,
    office, NAICS, incumbent, PWin, hold, outcome, open tasks, next due, next deadline."""
    with _conn() as conn:
        return workspace.pursuits(conn, stage=stage, include_closed=include_closed)


@server.tool()
def pursuit(pursuit_id: int) -> workspace.PursuitDetail:
    """One pursuit in full: its gate and whether it is ready, tasks, linked notices, the
    incumbent award and the prior solicitation's notices, government dates, and every
    recorded decision."""
    with _conn() as conn:
        return workspace.pursuit(conn, pursuit_id)


@server.tool()
def new_pursuit(
    title: str,
    summary: str | None = None,
    office_code: str | None = None,
    naics: str | None = None,
    notice_id: str | None = None,
    contract_id: int | None = None,
) -> workspace.Pursuit:
    """Open a pursuit in the workflow's first stage with its template tasks. A notice or an
    incumbent contract fills in the office and NAICS."""
    with _conn() as conn:
        return workspace.new_pursuit(
            conn, title, summary=summary, office_code=office_code, naics=naics,
            notice_id=notice_id, contract_id=contract_id,
        )  # fmt: skip


@server.tool()
def link_notice(pursuit_id: int, notice_id: str, role: str | None = None) -> workspace.LinkedNotice:
    """Attach a notice to a pursuit (role: solicitation, rfi, sources-sought, presolicitation,
    amendment, award, other; defaults from the notice type)."""
    with _conn() as conn:
        return workspace.link_notice(conn, pursuit_id, notice_id, role=role)


@server.tool()
def gate(pursuit_id: int, decision: str, why: str, until: str | None = None) -> workspace.Pursuit:
    """Record a gate decision with its rationale: go advances to the next stage, no-go closes
    the pursuit as no-bid, hold parks it until a date (YYYY-MM-DD)."""
    with _conn() as conn:
        return workspace.gate(conn, pursuit_id, decision, why, until=until)


@server.tool()
def tasks(
    subject_type: str | None = None, subject_id: str | None = None, open_only: bool = True
) -> list[workspace.Task]:
    """The user's tasks, most pressing first. A task is about a pursuit, an entity or a person,
    or about nothing at all; narrow with subject_type ('pursuit', 'entity', 'person') and
    subject_id, or pass neither for all of them."""
    with _conn() as conn:
        return list(
            workspace.tasks(
                conn, subject_type=subject_type, subject_id=subject_id, open_only=open_only
            )
        )


@server.tool()
def task_done(task_id: int) -> workspace.Task:
    """Mark a task done."""
    with _conn() as conn:
        return workspace.complete_task(conn, task_id)


@server.tool()
def update_pursuit(
    pursuit_id: int, pwin: int | None = None, notes: str | None = None
) -> workspace.Pursuit:
    """Set a pursuit's win probability (0 to 100) or replace its notes; both are recorded."""
    with _conn() as conn:
        return workspace.update_pursuit(conn, pursuit_id, pwin=pwin, notes=notes)


@server.tool()
def saved_searches() -> list[workspace.SavedSearch]:
    """The user's saved searches: name, optional text, and filters."""
    with _conn() as conn:
        return workspace.list_searches(conn)


@server.tool()
def run_saved_search(name: str, limit: int = 50) -> list[query.SearchHit]:
    """Run a saved search over active notices."""
    with _conn() as conn:
        return workspace.run_search(conn, name, limit=limit)


@server.tool()
def save_search(
    name: str,
    query_text: str | None = None,
    naics: list[str] | None = None,
    set_aside: list[str] | None = None,
    agency: list[str] | None = None,
    deadline_days: int | None = None,
) -> workspace.SavedSearch:
    """Create or replace a saved search. Matching notices are fetched first."""
    filters = query.Filters(
        naics=_tuple(naics),
        set_asides=_tuple(set_aside),
        agency_prefixes=_tuple(agency),
        deadline_within_days=deadline_days,
    )
    with _conn() as conn:
        return workspace.save_search(conn, name, query_text=query_text, filters=filters)


@server.tool()
def queue_status() -> queue.QueueStatus:
    """Pending description and attachment fetches, and the next descriptions in line."""
    with _conn() as conn:
        return queue.queue_status(conn)


@server.tool()
def quota_today() -> dict[str, object]:
    """Today's SAM.gov request budget (UTC day): spent, budget, remaining."""
    settings = Settings()
    with _conn() as conn:
        spent = quota.spent_today(conn)
    return {
        "date": datetime.now(UTC).date().isoformat(),
        "spent": spent,
        "budget": settings.sam_daily_budget,
        "remaining": max(settings.sam_daily_budget - spent, 0),
    }


@server.tool()
def profile() -> workspace.Profile | None:
    """The user's company profile, or null if none has been set."""
    with _conn() as conn:
        return workspace.get_profile(conn)


def main() -> None:
    server.run("stdio")
