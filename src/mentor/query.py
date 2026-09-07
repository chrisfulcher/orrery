"""The one query module every front end reads through (CLAUDE.md Interfaces; DESIGN.md §4).

Search unions hits from the notice text index and the attachment text index, keeps the
best-ranked hit per notice, and labels it with its source. bm25 scores from two tables are
not one scale; treating them as comparable is a ranking heuristic, not a measurement.
"""

import json
import sqlite3
from dataclasses import dataclass

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
