-- 0012_assessments: AI assessments of a pursuit, each with the provenance an assertion
-- needs: which model on which slot, the prompt version, the profile version it cited, a hash
-- of the inputs, the tokens spent, and the raw answer beside the validated one. Append-only.
-- Mirrors docs/DESIGN.md §8. Applied inside one transaction by mentor.db.migrate.

CREATE TABLE assessments (
    assessment_id   INTEGER PRIMARY KEY,
    pursuit_id      INTEGER NOT NULL REFERENCES pursuits(pursuit_id),
    user_id         INTEGER NOT NULL REFERENCES users(user_id),
    slot            TEXT NOT NULL CHECK (slot IN ('fast', 'deep')),
    provider        TEXT NOT NULL,           -- 'openai' or 'anthropic'
    model           TEXT NOT NULL,
    prompt_version  INTEGER NOT NULL,
    profile_version INTEGER,                 -- workspace_documents.version cited; NULL when none
    inputs_hash     TEXT NOT NULL,           -- sha256 of the rendered prompt
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    raw_response    TEXT NOT NULL,           -- the model's text as returned (last attempt)
    result          TEXT NOT NULL,           -- the validated assessment as JSON
    created_at      TEXT NOT NULL
);
CREATE INDEX assessments_pursuit ON assessments(user_id, pursuit_id, assessment_id);
CREATE TRIGGER assessments_append_only_update BEFORE UPDATE ON assessments
BEGIN SELECT RAISE(ABORT, 'assessments are append-only'); END;
CREATE TRIGGER assessments_append_only_delete BEFORE DELETE ON assessments
BEGIN SELECT RAISE(ABORT, 'assessments are append-only'); END;
