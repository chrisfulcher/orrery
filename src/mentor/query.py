"""The one query module every front end reads through (CLAUDE.md Interfaces; DESIGN.md §4).

Search unions hits from the notice text index and the attachment text index, keeps the
best-ranked hit per notice, and labels it with its source. bm25 scores from two tables are
not one scale; treating them as comparable is a ranking heuristic, not a measurement.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from mentor import db


class InvalidQuery(ValueError):
    """The text is not valid FTS5 syntax even after quoting each token."""


@dataclass(frozen=True)
class SearchHit:
    notice_id: str
    title: str
    agency: str | None
    response_deadline: str | None
    posted_at: str | None
    source: str
    """'notice' for the notice's own text, otherwise the attachment filename."""
    snippet: str
    rank: float
    """bm25 for keyword hits, cosine distance for semantic hits; lower is better in both."""
    page: int | None = None
    """1-based page of an attachment hit; None for notice text and for keyword hits."""


@dataclass(frozen=True)
class Filters:
    """Structured predicates over notices; None means any. Saved searches store one of these."""

    naics: tuple[str, ...] | None = None
    set_asides: tuple[str, ...] | None = None
    agency_prefixes: tuple[str, ...] | None = None
    """``full_parent_path_code`` prefixes, matched on dot boundaries."""
    deadline_within_days: int | None = None
    active_only: bool = False

    def params(self) -> dict[str, object]:
        return {
            "naics": json.dumps(self.naics) if self.naics else None,
            "set_asides": json.dumps(self.set_asides) if self.set_asides else None,
            "agency_path_prefixes": (
                json.dumps(self.agency_prefixes) if self.agency_prefixes else None
            ),
            "deadline_within_days": self.deadline_within_days,
            "active_only": int(self.active_only),
            "now": db.utcnow(),
        }


NO_FILTERS = Filters()


def notice_filter_sql(src: str) -> str:
    """Predicates on ``notices AS n`` reading filter values from ``src``: ``:`` for bound
    parameters, or a table alias such as ``s.`` whose columns carry the same names."""
    return f"""
    AND ({src}naics IS NULL OR n.naics_code IN (SELECT value FROM json_each({src}naics)))
    AND ({src}set_asides IS NULL
         OR n.set_aside_code IN (SELECT value FROM json_each({src}set_asides)))
    AND ({src}agency_path_prefixes IS NULL OR EXISTS (
         SELECT 1 FROM json_each({src}agency_path_prefixes)
         WHERE n.full_parent_path_code = value OR n.full_parent_path_code LIKE value || '.%'))
    AND ({src}deadline_within_days IS NULL OR n.response_deadline BETWEEN :now AND
         strftime('%Y-%m-%dT%H:%M:%SZ', :now, '+' || {src}deadline_within_days || ' days'))
    """


FILTERS = notice_filter_sql(":") + " AND (:active_only = 0 OR n.active = 1)"

SEARCH = f"""
WITH hits AS (
    SELECT n.id AS nid, 'notice' AS source, bm25(notices_fts) AS rank,
           snippet(notices_fts, -1, '[', ']', '...', 12) AS snippet
    FROM notices_fts JOIN notices AS n ON n.id = notices_fts.rowid
    WHERE notices_fts MATCH :q
    UNION ALL
    SELECT n.id, a.filename, bm25(attachments_fts),
           snippet(attachments_fts, 0, '[', ']', '...', 12)
    FROM attachments_fts
    JOIN attachments AS a ON a.attachment_id = attachments_fts.rowid
    JOIN notices AS n ON n.notice_id = a.notice_id
    WHERE attachments_fts MATCH :q
)
SELECT n.notice_id, n.title, e.name, n.response_deadline, n.posted_at,
       h.source, h.snippet, min(h.rank) AS rank
FROM hits AS h JOIN notices AS n ON n.id = h.nid
LEFT JOIN entities AS e ON e.entity_id = n.agency_entity_id
WHERE 1 = 1 {FILTERS}
GROUP BY h.nid
ORDER BY rank, n.posted_at DESC
LIMIT :limit
"""

LIST_NOTICES = f"""
SELECT n.notice_id, n.title, e.name, n.response_deadline, n.posted_at,
       'notice' AS source, coalesce(n.description, '') AS snippet, 0.0 AS rank
FROM notices AS n LEFT JOIN entities AS e ON e.entity_id = n.agency_entity_id
WHERE 1 = 1 {FILTERS}
ORDER BY n.response_deadline IS NULL, n.response_deadline, n.posted_at DESC, n.id
LIMIT :limit
"""


SEMANTIC_SEARCH = """
WITH hits AS (
    SELECT notice_id, attachment_id, page, text, vec_distance_cosine(vector, :q) AS distance
    FROM embeddings WHERE model = :model
)
SELECT n.notice_id, n.title, e.name, n.response_deadline, n.posted_at,
       coalesce(a.filename, 'notice') AS source, h.text, min(h.distance) AS rank, h.page
FROM hits AS h
JOIN notices AS n ON n.notice_id = h.notice_id
LEFT JOIN attachments AS a ON a.attachment_id = h.attachment_id
LEFT JOIN entities AS e ON e.entity_id = n.agency_entity_id
GROUP BY h.notice_id
ORDER BY rank, n.posted_at DESC
LIMIT :limit
"""


def semantic_search(
    conn: sqlite3.Connection, vector: bytes, *, model: str, limit: int = 20
) -> list[SearchHit]:
    """Nearest chunk per notice by cosine distance over rows embedded with ``model``.

    ``vector`` is the query embedding as a float32 blob (``mentor.embed.client.pack``); the
    caller embeds the query, so this module never touches the network. The connection must
    have ``db.load_vec`` applied.
    """
    rows = conn.execute(SEMANTIC_SEARCH, {"q": vector, "model": model, "limit": limit}).fetchall()
    return [SearchHit(*row[:6], _snippet(row[6]), row[7], row[8]) for row in rows]


def _snippet(text: str, width: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[:width].rsplit(" ", 1)[0] + "..."


def search(
    conn: sqlite3.Connection, text: str, *, limit: int = 20, filters: Filters = NO_FILTERS
) -> list[SearchHit]:
    """Full-text search across notice text and attachment text, best hit per notice.

    The text is passed to FTS5 as written first, so operators, prefixes, and phrases work;
    if FTS5 rejects it, every whitespace-separated token is retried as its own phrase, so
    plain queries such as ``wi-fi`` or ``section l/m`` work too. ``filters`` narrows the
    notices considered.
    """
    try:
        rows = _match(conn, text, limit, filters)
    except sqlite3.OperationalError:
        try:
            rows = _match(conn, _quoted(text), limit, filters)
        except sqlite3.OperationalError as exc:
            raise InvalidQuery(str(exc)) from exc
    return [SearchHit(*row[:6], " ".join(row[6].split()), row[7]) for row in rows]


def list_notices(conn: sqlite3.Connection, filters: Filters, *, limit: int = 50) -> list[SearchHit]:
    """Notices passing the filters, soonest deadline first; no text ranking (rank is 0)."""
    rows = conn.execute(LIST_NOTICES, {"limit": limit, **filters.params()}).fetchall()
    return [SearchHit(*row[:6], _snippet(row[6]), row[7]) for row in rows]


def rebuild_search(conn: sqlite3.Connection) -> None:
    """Rebuild both FTS indexes from their content tables."""
    conn.execute("INSERT INTO notices_fts(notices_fts) VALUES ('rebuild')")
    conn.execute("INSERT INTO attachments_fts(attachments_fts) VALUES ('rebuild')")


def _match(conn: sqlite3.Connection, query: str, limit: int, filters: Filters) -> list[tuple]:
    return conn.execute(SEARCH, {"q": query, "limit": limit, **filters.params()}).fetchall()


def _quoted(text: str) -> str:
    return " ".join('"' + token.replace('"', '""') + '"' for token in text.split())


# Detail reads for the context view, the entity view, the dashboard, and the MCP server.
# Public data only; tracking lives in mentor.workspace.


@dataclass(frozen=True)
class EntityRef:
    entity_id: int
    name: str
    path_code: str | None


@dataclass(frozen=True)
class AttachmentInfo:
    attachment_id: int
    filename: str | None
    url: str
    fetch_status: str
    extract_status: str
    path: str | None
    text_chars: int


@dataclass(frozen=True)
class NoticeDetail:
    notice_id: str
    solicitation_number: str | None
    title: str
    notice_type: str | None
    naics_code: str | None
    psc_code: str | None
    set_aside_code: str | None
    posted_at: str | None
    response_deadline: str | None
    active: bool
    first_seen_at: str
    last_seen_at: str
    source_id: str
    description_status: str
    description: str | None
    url: str | None
    agency: str | None
    agency_chain: tuple[EntityRef, ...]
    """Root first, leaf last."""
    attachments: tuple[AttachmentInfo, ...]
    versions: int


@dataclass(frozen=True)
class EntityDetail:
    entity_id: int
    kind: str
    name: str
    path_code: str | None
    parent: EntityRef | None
    chain: tuple[EntityRef, ...]
    """Root first, this entity last."""
    children: tuple[EntityRef, ...]
    aliases: tuple[str, ...]
    notices: int
    """Notices under this entity or any office below it."""
    recent: tuple[SearchHit, ...]


@dataclass(frozen=True)
class StoreCounts:
    notices: int
    active: int
    entities: int


CHAIN = """
WITH RECURSIVE chain(entity_id, name, agency_path_code, parent_entity_id, depth) AS (
    SELECT entity_id, name, agency_path_code, parent_entity_id, 0
    FROM entities WHERE entity_id = :id
    UNION ALL
    SELECT e.entity_id, e.name, e.agency_path_code, e.parent_entity_id, c.depth + 1
    FROM entities AS e JOIN chain AS c ON e.entity_id = c.parent_entity_id
)
SELECT entity_id, name, agency_path_code FROM chain ORDER BY depth DESC
"""


def notice(conn: sqlite3.Connection, notice_id: str) -> NoticeDetail | None:
    """Everything public about one notice, for the context view."""
    row = conn.execute(
        "SELECT notice_id, solicitation_number, title, notice_type, naics_code, psc_code,"
        " set_aside_code, posted_at, response_deadline, active, first_seen_at, last_seen_at,"
        " source_id, description_status, description, url, agency, agency_entity_id, versions"
        " FROM v_notices WHERE notice_id = ?",
        (notice_id,),
    ).fetchone()
    if row is None:
        return None
    attachments = tuple(
        AttachmentInfo(*item)
        for item in conn.execute(
            "SELECT attachment_id, filename, url, fetch_status, extract_status, path,"
            " length(coalesce(extracted_text, '')) FROM attachments"
            " WHERE notice_id = ? ORDER BY attachment_id",
            (notice_id,),
        ).fetchall()
    )
    return NoticeDetail(
        *row[:9],
        bool(row[9]),
        *row[10:17],
        _chain(conn, row[17]),
        attachments,
        row[18],
    )


def entity(conn: sqlite3.Connection, entity_id: int, *, recent: int = 10) -> EntityDetail | None:
    """One agency or office with its place in the hierarchy and its recent notices."""
    row = conn.execute(
        "SELECT entity_id, kind, name, agency_path_code, parent_entity_id, parent, notices"
        " FROM v_entities WHERE entity_id = ?",
        (entity_id,),
    ).fetchone()
    if row is None:
        return None
    children = tuple(
        EntityRef(*item)
        for item in conn.execute(
            "SELECT entity_id, name, agency_path_code FROM entities"
            " WHERE parent_entity_id = ? ORDER BY name",
            (entity_id,),
        ).fetchall()
    )
    aliases = tuple(
        alias
        for (alias,) in conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id = ? ORDER BY alias", (entity_id,)
        ).fetchall()
    )
    chain = _chain(conn, entity_id)
    parent = chain[-2] if len(chain) > 1 else None
    hits = list_notices(conn, Filters(agency_prefixes=(row[3],)), limit=recent) if row[3] else []
    return EntityDetail(
        row[0], row[1], row[2], row[3], parent, chain, children, aliases, row[6], tuple(hits)
    )


def upcoming(conn: sqlite3.Connection, *, days: int = 7, limit: int = 50) -> list[SearchHit]:
    """Active notices due within ``days``, soonest first."""
    return list_notices(conn, Filters(deadline_within_days=days, active_only=True), limit=limit)


def activity(conn: sqlite3.Connection, *, days: int = 14) -> list[tuple[str, int]]:
    """Notices first seen per UTC day, one entry per day ending today, oldest first."""
    return _daily(
        conn,
        "SELECT date(first_seen_at) AS day, count(*) FROM notices"
        " WHERE date(first_seen_at) >= :start GROUP BY day",
        days,
    )


def quota_history(conn: sqlite3.Connection, *, days: int = 30) -> list[tuple[str, int]]:
    """Keyed SAM.gov requests per UTC day, one entry per day ending today, oldest first."""
    return _daily(conn, "SELECT day, requests FROM v_quota_daily WHERE day >= :start", days)


def counts(conn: sqlite3.Connection) -> StoreCounts:
    (notices, active) = conn.execute("SELECT count(*), sum(active) FROM notices").fetchone()
    (entities,) = conn.execute("SELECT count(*) FROM entities").fetchone()
    return StoreCounts(notices, active or 0, entities)


def _chain(conn: sqlite3.Connection, entity_id: int | None) -> tuple[EntityRef, ...]:
    if entity_id is None:
        return ()
    return tuple(EntityRef(*row) for row in conn.execute(CHAIN, {"id": entity_id}).fetchall())


def _daily(conn: sqlite3.Connection, sql: str, days: int) -> list[tuple[str, int]]:
    today = datetime.strptime(db.utcnow(), db.TIMESTAMP_FORMAT).date()
    start = today - timedelta(days=days - 1)
    found = dict(conn.execute(sql, {"start": start.isoformat()}).fetchall())
    series = []
    for offset in range(days):
        day = (start + timedelta(days=offset)).isoformat()
        series.append((day, found.get(day, 0)))
    return series
