-- 0004_embeddings: one row per embedded chunk of notice or attachment text.
-- Derived and rebuildable: DELETE FROM embeddings WHERE model = ?; then `mentor embed`.
-- Mirrors docs/DESIGN.md §8. Applied inside one transaction by mentor.db.migrate.

CREATE TABLE embeddings (
    embedding_id  INTEGER PRIMARY KEY,
    notice_id     TEXT NOT NULL REFERENCES notices(notice_id),
    attachment_id INTEGER REFERENCES attachments(attachment_id),   -- NULL: a description chunk
    chunk_index   INTEGER NOT NULL,
    page          INTEGER,        -- 1-based, from the form-feed page separators; NULL for descriptions
    text          TEXT NOT NULL,  -- the chunk exactly as sent to the model
    model         TEXT NOT NULL,  -- MENTOR_EMBED_MODEL at embed time; re-embedding is per model
    vector        BLOB NOT NULL,  -- float32 little-endian, raw; length(vector) / 4 = dimensions
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
-- SQLite treats NULL as distinct in UNIQUE, so a description chunk (attachment_id NULL) is
-- folded to 0. The index also serves the "already embedded under this model?" lookups.
CREATE UNIQUE INDEX embeddings_chunk
    ON embeddings(model, notice_id, coalesce(attachment_id, 0), chunk_index);
