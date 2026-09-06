"""Embed every fetched description and extracted attachment that lacks vectors for the
configured model. Spends no SAM.gov quota; contacts only the configured endpoint.
Serves docs/DESIGN.md §8.

One transaction per source: the endpoint is called before BEGIN, so a failure inserts nothing
for that source and completed sources stand. An endpoint error ends the run. A source whose
text yields no chunks (whitespace only) is reselected on every run at no cost.
"""

import sqlite3
from dataclasses import dataclass

from mentor.config import Settings
from mentor.embed.chunks import chunk_text
from mentor.embed.client import EmbeddingClient, pack

PENDING_NOTICES = """
SELECT n.notice_id, n.description FROM notices AS n
WHERE n.description_status = 'fetched' AND n.description <> ''
  AND NOT EXISTS (SELECT 1 FROM embeddings AS e WHERE e.model = :model
                  AND e.notice_id = n.notice_id AND e.attachment_id IS NULL)
ORDER BY n.id LIMIT :limit
"""

PENDING_ATTACHMENTS = """
SELECT a.attachment_id, a.notice_id, a.extracted_text FROM attachments AS a
WHERE a.extract_status = 'done' AND a.extracted_text <> ''
  AND NOT EXISTS (SELECT 1 FROM embeddings AS e WHERE e.model = :model
                  AND e.notice_id = a.notice_id AND e.attachment_id = a.attachment_id)
ORDER BY a.attachment_id LIMIT :limit
"""


@dataclass(frozen=True)
class EmbedResult:
    notices: int
    attachments: int
    chunks: int
    model: str


def embed_pending(
    conn: sqlite3.Connection, settings: Settings, *, limit: int | None = None
) -> EmbedResult:
    """Chunk and embed pending sources, notices then attachments. ``limit`` caps sources."""
    model = settings.embed_model
    notices = attachments = chunks = 0
    with EmbeddingClient(settings) as client:
        rows = conn.execute(
            PENDING_NOTICES, {"model": model, "limit": -1 if limit is None else limit}
        ).fetchall()
        for notice_id, text in rows:
            chunks += _embed_source(conn, client, model, notice_id, None, text)
            notices += 1
        remaining = -1 if limit is None else limit - notices
        rows = conn.execute(PENDING_ATTACHMENTS, {"model": model, "limit": remaining}).fetchall()
        for attachment_id, notice_id, text in rows:
            chunks += _embed_source(conn, client, model, notice_id, attachment_id, text)
            attachments += 1
    return EmbedResult(notices, attachments, chunks, model)


def _embed_source(
    conn: sqlite3.Connection,
    client: EmbeddingClient,
    model: str,
    notice_id: str,
    attachment_id: int | None,
    text: str,
) -> int:
    pieces = chunk_text(text)
    if not pieces:
        return 0
    vectors = client.embed([piece.text for piece in pieces])  # before BEGIN: an error opens nothing
    conn.execute("BEGIN")
    try:
        conn.executemany(
            "INSERT INTO embeddings (notice_id, attachment_id, chunk_index, page, text, model,"
            " vector) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    notice_id,
                    attachment_id,
                    piece.index,
                    piece.page if attachment_id is not None else None,
                    piece.text,
                    model,
                    pack(vector),
                )
                for piece, vector in zip(pieces, vectors, strict=True)
            ],
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return len(pieces)
