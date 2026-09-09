-- 0013_notice_summaries: a two-line summary and tags per notice written by a chat slot, each
-- row with the provenance an assertion needs: the slot and provider, the model, the prompt
-- version, a hash of the rendered inputs, the tokens spent, and the raw answer beside the
-- validated one. A derived layer over notices, rebuildable like embeddings: rows are never
-- updated, and a rebuild deletes a model's rows and runs `orrery summarize` again. An answer
-- that failed validation is recorded as a row with a NULL result so it is not retried under
-- the same model and prompt version (as a failed description fetch is terminal).
-- Mirrors docs/DESIGN.md §8. Applied inside one transaction by orrery.db.migrate.

CREATE TABLE notice_summaries (
    summary_id       INTEGER PRIMARY KEY,
    notice_id        TEXT NOT NULL REFERENCES notices(notice_id),
    slot             TEXT NOT NULL CHECK (slot IN ('fast', 'deep')),
    provider         TEXT NOT NULL,           -- 'openai' or 'anthropic'
    model            TEXT NOT NULL,
    prompt_version   INTEGER NOT NULL,
    inputs_hash      TEXT NOT NULL,           -- sha256 of the rendered prompt
    input_tokens     INTEGER,
    output_tokens    INTEGER,
    raw_response     TEXT NOT NULL,           -- the model's text as returned (last attempt)
    result           TEXT,                    -- the validated answer as JSON; NULL when the
                                              -- answer failed validation
    summary          TEXT,                    -- projections of result; NULL with it
    work_type        TEXT,
    keywords         TEXT CHECK (keywords IS NULL OR json_type(keywords) = 'array'),
    stated_set_aside TEXT,                    -- the set-aside the text states (SAM.gov code)
    created_at       TEXT NOT NULL
);
-- Serves both "latest per notice" and "done under this model and prompt?" lookups.
CREATE INDEX notice_summaries_notice ON notice_summaries(notice_id, summary_id);
CREATE TRIGGER notice_summaries_no_update BEFORE UPDATE ON notice_summaries
BEGIN SELECT RAISE(ABORT, 'notice_summaries rows are never updated'); END;

-- The latest valid summary per notice, whichever model wrote it (interface version 1).
CREATE VIEW v_notice_summaries AS
SELECT s.notice_id, s.summary, s.work_type, s.keywords, s.stated_set_aside, s.slot, s.model,
       s.prompt_version, s.created_at
FROM notice_summaries AS s
WHERE s.summary_id = (SELECT max(l.summary_id) FROM notice_summaries AS l
                      WHERE l.notice_id = s.notice_id AND l.result IS NOT NULL);

-- v_notices gains the latest summary. Every 0007 column is kept in order.
DROP VIEW v_notices;
CREATE VIEW v_notices AS
SELECT n.notice_id, n.solicitation_number, n.title, n.notice_type, n.naics_code, n.psc_code,
       n.set_aside_code, n.posted_at, n.response_deadline, n.active,
       n.first_seen_at, n.last_seen_at, n.source_id,
       n.full_parent_path_code AS agency_path_code, n.full_parent_path_name AS agency_path_name,
       n.agency_entity_id, e.name AS agency,
       n.description_status, n.description,
       coalesce(json_extract(n.raw_json, '$.uiLink'), json_extract(n.raw_json, '$.Link')) AS url,
       (SELECT count(*) FROM attachments AS a WHERE a.notice_id = n.notice_id) AS attachments,
       (SELECT count(*) FROM attachments AS a
         WHERE a.notice_id = n.notice_id AND a.fetch_status = 'fetched') AS attachments_fetched,
       (SELECT count(*) FROM attachments AS a
         WHERE a.notice_id = n.notice_id AND a.extract_status = 'done') AS attachments_extracted,
       (SELECT count(*) FROM notice_versions AS v WHERE v.notice_id = n.notice_id) AS versions,
       coalesce(json_extract(n.raw_json, '$.award.number'),
                nullif(json_extract(n.raw_json, '$.AwardNumber'), '')) AS award_number,
       coalesce(json_extract(n.raw_json, '$.award.date'),
                nullif(json_extract(n.raw_json, '$.AwardDate'), '')) AS award_date,
       coalesce(json_extract(n.raw_json, '$.award.amount'),
                nullif(json_extract(n.raw_json, '$."Award$"'), '')) AS award_amount,
       coalesce(json_extract(n.raw_json, '$.award.awardee.name'),
                nullif(json_extract(n.raw_json, '$.Awardee'), '')) AS awardee,
       s.summary, s.work_type, s.keywords, s.stated_set_aside, s.model AS summary_model
FROM notices AS n
LEFT JOIN entities AS e ON e.entity_id = n.agency_entity_id
LEFT JOIN v_notice_summaries AS s ON s.notice_id = n.notice_id;
