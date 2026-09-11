-- 0018_tasks: a task is no longer only about a pursuit. pursuit_tasks.pursuit_id was NOT NULL
-- and a foreign key, so "call the contracting officer at this office before the industry day"
-- or "check whether this incumbent's registration has lapsed" had to be hung off a pursuit
-- that may not exist, or kept somewhere else entirely -- which for most people means somewhere
-- else entirely. The subject becomes polymorphic in the style facts already uses at scale:
-- a subject_type from a closed vocabulary plus a subject_id, both NULL together for a
-- standalone to-do. 'requirement' is deliberately absent until #7 makes one writable; a CHECK
-- listing a member nothing can write is a lie about the store, and widening it later is the
-- cheap move that made this shape the choice.
--
-- Three consequences to keep in mind when reading anything that joins this table:
--   * subject_id is TEXT, matching facts.subject_id. Every query against a pursuit MUST also
--     say subject_type = 'pursuit'; without it an entity whose entity_id happens to equal a
--     pursuit_id matches too, silently and without error.
--   * referential integrity is given up here and replaced by one write-time chokepoint,
--     workspace._resolve_subject.
--   * stage is nullable and belongs to a pursuit's workflow. That is a domain rule, enforced
--     in _resolve_subject rather than welded into a CHECK a later subject kind would have to
--     rebuild the table to relax.
--
-- precedence is #30's scale, stored and displayed only: nothing derives it and nothing emits
-- an alert task yet. NULL means the user has not said -- not a fifth level and not a synonym
-- for 'routine'. #30's rule is that a user's assignment wins as a visible override of a
-- derived default, and that sentence only parses while unset stays distinguishable from a
-- chosen 'routine'. origin gains 'alert' now so emitted work is native rather than bolted on.
--
-- Nothing REFERENCES pursuit_tasks, so this rebuild needs no foreign-key dance -- which is
-- just as well, since PRAGMA foreign_keys is a no-op inside the transaction db.migrate opens.
-- Mirrors docs/DESIGN.md §8. Applied inside one transaction by orrery.db.migrate; do not add
-- BEGIN/COMMIT.

DROP VIEW v_pursuit_tasks;
DROP VIEW v_pursuits;

CREATE TABLE tasks (
    task_id      INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(user_id),
    subject_type TEXT CHECK (subject_type IN ('pursuit', 'entity', 'person')),
    subject_id   TEXT,                             -- TEXT like facts.subject_id, not a FK
    stage        TEXT,                             -- a key of the workflow document, pursuits only
    title        TEXT NOT NULL,
    origin       TEXT NOT NULL CHECK (origin IN ('template', 'user', 'alert')),
    precedence   TEXT CHECK (precedence IN ('flash', 'immediate', 'priority', 'routine')),
    due          TEXT,                             -- YYYY-MM-DD, the user's date
    done_at      TEXT,
    created_at   TEXT NOT NULL,
    CHECK ((subject_type IS NULL) = (subject_id IS NULL))
);
CREATE INDEX tasks_open ON tasks(user_id, done_at, due);
CREATE INDEX tasks_subject ON tasks(user_id, subject_type, subject_id);

-- task_id is copied, not reissued: it is the address space `orrery task done 47`, the terminal
-- UI's row keys and the MCP task_done tool all name. INTEGER PRIMARY KEY without AUTOINCREMENT
-- continues from max(rowid), so new tasks carry on from the highest copied id.
INSERT INTO tasks (task_id, user_id, subject_type, subject_id, stage, title, origin,
                   precedence, due, done_at, created_at)
SELECT task_id, user_id, 'pursuit', pursuit_id, stage, title, origin,
       NULL, due, done_at, created_at
FROM pursuit_tasks ORDER BY task_id;

-- Refuse to drop the old table unless every row arrived. The CHECK fails on a short copy,
-- which raises, which rolls the whole migration back. This is the first rebuild of a table
-- holding work the user typed, and a silent partial copy would be unrecoverable.
CREATE TEMP TABLE _copied (ok INTEGER CHECK (ok = 1));
INSERT INTO _copied SELECT (SELECT count(*) FROM tasks) = (SELECT count(*) FROM pursuit_tasks);
DROP TABLE _copied;

DROP TABLE pursuit_tasks;

-- Views (interface version 1): v_pursuits and v_pursuit_tasks keep every column they had.
-- Both gain the subject_type filter their old join gave them for free.
CREATE VIEW v_pursuits AS
SELECT p.user_id, p.pursuit_id, p.title, p.summary, p.stage, p.pwin, p.notes, p.held_until,
       p.outcome, p.closed_at, p.office_entity_id, o.name AS office, p.office_code, p.naics_code,
       p.incumbent_contract_id, coalesce(v.name, c.recipient_name) AS incumbent,
       c.pop_end AS incumbent_pop_end,
       (SELECT count(*) FROM pursuit_notices AS pn WHERE pn.pursuit_id = p.pursuit_id) AS notices,
       (SELECT count(*) FROM tasks AS t
         WHERE t.subject_type = 'pursuit' AND t.subject_id = CAST(p.pursuit_id AS TEXT)
           AND t.done_at IS NULL) AS open_tasks,
       (SELECT min(t.due) FROM tasks AS t
         WHERE t.subject_type = 'pursuit' AND t.subject_id = CAST(p.pursuit_id AS TEXT)
           AND t.done_at IS NULL AND t.due IS NOT NULL) AS next_due,
       (SELECT min(n.response_deadline) FROM pursuit_notices AS pn
         JOIN notices AS n ON n.notice_id = pn.notice_id
         WHERE pn.pursuit_id = p.pursuit_id
           AND n.response_deadline >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now')) AS next_response_deadline,
       (SELECT max(e.changed_at) FROM pursuit_events AS e
         WHERE e.pursuit_id = p.pursuit_id) AS last_event_at,
       p.created_at, p.updated_at
FROM pursuits AS p
LEFT JOIN entities AS o ON o.entity_id = p.office_entity_id
LEFT JOIN contracts AS c ON c.contract_id = p.incumbent_contract_id
LEFT JOIN entities AS v ON v.entity_id = c.vendor_entity_id;

CREATE VIEW v_pursuit_tasks AS
SELECT t.user_id, t.task_id, CAST(t.subject_id AS INTEGER) AS pursuit_id,
       p.title AS pursuit_title, p.stage AS pursuit_stage, p.outcome, p.closed_at,
       t.stage, t.title, t.origin, t.precedence, t.due, t.done_at, t.created_at
FROM tasks AS t JOIN pursuits AS p ON p.pursuit_id = CAST(t.subject_id AS INTEGER)
WHERE t.subject_type = 'pursuit';

-- Every task, whatever it is about. pursuit_id is NULL for every other subject, so a consumer
-- that only wants pursuit work can filter on it without knowing the vocabulary; subject is the
-- resolved display label, NULL for a standalone to-do.
CREATE VIEW v_tasks AS
SELECT t.user_id, t.task_id, t.subject_type, t.subject_id,
       CASE t.subject_type
           WHEN 'pursuit' THEN p.title
           WHEN 'entity'  THEN e.name
           WHEN 'person'  THEN pe.name
       END AS subject,
       CASE WHEN t.subject_type = 'pursuit' THEN CAST(t.subject_id AS INTEGER) END AS pursuit_id,
       t.stage, t.title, t.origin, t.precedence, t.due, t.done_at, t.created_at
FROM tasks AS t
LEFT JOIN pursuits AS p
       ON t.subject_type = 'pursuit' AND p.pursuit_id = CAST(t.subject_id AS INTEGER)
LEFT JOIN entities AS e
       ON t.subject_type = 'entity' AND e.entity_id = CAST(t.subject_id AS INTEGER)
LEFT JOIN people AS pe
       ON t.subject_type = 'person' AND pe.person_id = CAST(t.subject_id AS INTEGER);
