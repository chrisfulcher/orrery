"""MCP server: the store for AI agents, over stdio. Interface version 1 (DESIGN.md §4).

Tools are thin wrappers over the query and workspace modules and return the engine's own
records. Reads plus two cheap writes (track, save_search). Nothing here spends SAM.gov quota
or contacts the network: ingest, fetch, and embed are deliberately not tools. Tools and
their fields are only ever added.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime

from mcp.server.mcpserver import MCPServer

from mentor import __version__, db, query, quota, workspace
from mentor.config import Settings
from mentor.fetch import queue

INTERFACE_VERSION = 1

server = MCPServer(
    "mentor",
    version=__version__,
    instructions=(
        f"mentor MCP interface v{INTERFACE_VERSION}: a local store of U.S. federal contract"
        " opportunities (SAM.gov notices, their documents, agencies, and the user's pipeline)."
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
    """Keyword search across notice text and attachment text (FTS5 syntax allowed); one
    best hit per notice with its source and snippet. Filters: NAICS codes, set-aside codes,
    agency path-code prefixes, and a deadline window in days."""
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
    attachments with fetch and extraction status, version count, and the SAM.gov page URL."""
    with _conn() as conn:
        detail = query.notice(conn, notice_id)
    if detail is None:
        raise workspace.NotFound(f"no notice {notice_id}")
    return detail


@server.tool()
def entity(entity_id: int) -> query.EntityDetail:
    """One agency or office: its place in the hierarchy, children, aliases, notice count,
    and recent notices."""
    with _conn() as conn:
        detail = query.entity(conn, entity_id)
    if detail is None:
        raise workspace.NotFound(f"no entity {entity_id}")
    return detail


@server.tool()
def upcoming(days: int = 7, limit: int = 50) -> list[query.SearchHit]:
    """Active notices with a response deadline within the next N days, soonest first."""
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
