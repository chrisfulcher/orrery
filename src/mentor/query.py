"""The one query module every front end reads through (CLAUDE.md Interfaces; DESIGN.md §4).

Search unions hits from the notice text index and the attachment text index, keeps the
best-ranked hit per notice, and labels it with its source. bm25 scores from two tables are
not one scale; treating them as comparable is a ranking heuristic, not a measurement.
"""

import sqlite3
from dataclasses import dataclass


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
    """bm25; lower is better."""


SEARCH = """
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
GROUP BY h.nid
ORDER BY rank, n.posted_at DESC
LIMIT :limit
"""


def search(conn: sqlite3.Connection, text: str, *, limit: int = 20) -> list[SearchHit]:
    """Full-text search across notice text and attachment text, best hit per notice.

    The text is passed to FTS5 as written first, so operators, prefixes, and phrases work;
    if FTS5 rejects it, every whitespace-separated token is retried as its own phrase, so
    plain queries such as ``wi-fi`` or ``section l/m`` work too.
    """
    try:
        rows = _match(conn, text, limit)
    except sqlite3.OperationalError:
        try:
            rows = _match(conn, _quoted(text), limit)
        except sqlite3.OperationalError as exc:
            raise InvalidQuery(str(exc)) from exc
    return [SearchHit(*row[:6], " ".join(row[6].split()), row[7]) for row in rows]


def rebuild_search(conn: sqlite3.Connection) -> None:
    """Rebuild both FTS indexes from their content tables."""
    conn.execute("INSERT INTO notices_fts(notices_fts) VALUES ('rebuild')")
    conn.execute("INSERT INTO attachments_fts(attachments_fts) VALUES ('rebuild')")


def _match(conn: sqlite3.Connection, query: str, limit: int) -> list[tuple]:
    return conn.execute(SEARCH, {"q": query, "limit": limit}).fetchall()


def _quoted(text: str) -> str:
    return " ".join('"' + token.replace('"', '""') + '"' for token in text.split())
