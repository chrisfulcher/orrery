-- 0009_documents: versioned workspace documents (the company profile and the workflow as the
-- user wrote them, in TOML). Mirrors docs/DESIGN.md §8. Applied inside one transaction by
-- mentor.db.migrate; do not add BEGIN/COMMIT.

-- Append-only: every save is a new version, so an assessment can cite the profile it was
-- made against and a workflow change never rewrites history.
CREATE TABLE workspace_documents (
    document_id INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(user_id),
    kind        TEXT NOT NULL CHECK (kind IN ('profile', 'workflow')),
    version     INTEGER NOT NULL CHECK (version > 0),
    body        TEXT NOT NULL,   -- TOML as the user wrote it, comments included
    created_at  TEXT NOT NULL,
    UNIQUE (user_id, kind, version)
);
CREATE TRIGGER workspace_documents_append_only_update BEFORE UPDATE ON workspace_documents
BEGIN SELECT RAISE(ABORT, 'workspace_documents are append-only'); END;
CREATE TRIGGER workspace_documents_append_only_delete BEFORE DELETE ON workspace_documents
BEGIN SELECT RAISE(ABORT, 'workspace_documents are append-only'); END;
