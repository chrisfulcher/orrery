-- 0010_pursuits: the BD workflow. A pursuit is anchored on a requirement (an office and a
-- need), moves through the stages of the user's workflow document by recorded decisions,
-- and collects notices, tasks, and events. Mirrors docs/DESIGN.md §8. Applied inside one
-- transaction by mentor.db.migrate; do not add BEGIN/COMMIT.

CREATE TABLE pursuits (
    pursuit_id            INTEGER PRIMARY KEY,
    user_id               INTEGER NOT NULL REFERENCES users(user_id),
    title                 TEXT NOT NULL,
    summary               TEXT,                    -- the need, in the user's words
    office_entity_id      INTEGER REFERENCES entities(entity_id),
    office_code           TEXT,                    -- the AAC, kept even when unresolved
    naics_code            TEXT,
    incumbent_contract_id INTEGER REFERENCES contracts(contract_id),
    stage                 TEXT NOT NULL,           -- a key of the workflow document
    pwin                  INTEGER CHECK (pwin BETWEEN 0 AND 100),
    notes                 TEXT,
    held_until            TEXT,                    -- YYYY-MM-DD while parked at a gate
    outcome               TEXT CHECK (outcome IN ('no-bid', 'lost', 'won')),
    closed_at             TEXT,                    -- open while NULL; never deleted
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);
CREATE INDEX pursuits_open ON pursuits(user_id, closed_at, stage);

-- The notices a pursuit collects: its solicitation, an RFI, a sources sought, amendments.
CREATE TABLE pursuit_notices (
    pursuit_id INTEGER NOT NULL REFERENCES pursuits(pursuit_id),
    user_id    INTEGER NOT NULL REFERENCES users(user_id),
    notice_id  TEXT NOT NULL REFERENCES notices(notice_id),
    role       TEXT NOT NULL CHECK (role IN ('solicitation', 'rfi', 'sources-sought',
                                             'presolicitation', 'amendment', 'award', 'other')),
    linked_at  TEXT NOT NULL,
    PRIMARY KEY (pursuit_id, notice_id)
);
CREATE INDEX pursuit_notices_notice ON pursuit_notices(user_id, notice_id);

-- The user's work: seeded from the stage template on entry, or added by hand. Government
-- dates are never copied here; they are read from linked notices and the incumbent contract.
CREATE TABLE pursuit_tasks (
    task_id    INTEGER PRIMARY KEY,
    pursuit_id INTEGER NOT NULL REFERENCES pursuits(pursuit_id),
    user_id    INTEGER NOT NULL REFERENCES users(user_id),
    stage      TEXT NOT NULL,
    title      TEXT NOT NULL,
    origin     TEXT NOT NULL CHECK (origin IN ('template', 'user')),
    due        TEXT,                                -- YYYY-MM-DD, the user's date
    done_at    TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX pursuit_tasks_open ON pursuit_tasks(user_id, done_at, due);

-- Append-only: every decision with its rationale, like tracked_opportunity_events.
CREATE TABLE pursuit_events (
    event_id   INTEGER PRIMARY KEY,
    pursuit_id INTEGER NOT NULL REFERENCES pursuits(pursuit_id),
    user_id    INTEGER NOT NULL REFERENCES users(user_id),
    changed_at TEXT NOT NULL,
    field      TEXT NOT NULL CHECK (field IN ('stage', 'pwin', 'notes', 'gate', 'hold',
                                              'outcome', 'task', 'notice', 'closed', 'reopened')),
    old_value  TEXT,
    new_value  TEXT,
    note       TEXT                                 -- the rationale, task title, or notice role
);
CREATE INDEX pursuit_events_pursuit ON pursuit_events(pursuit_id, event_id);
CREATE TRIGGER pursuit_events_append_only_update BEFORE UPDATE ON pursuit_events
BEGIN SELECT RAISE(ABORT, 'pursuit_events are append-only'); END;
CREATE TRIGGER pursuit_events_append_only_delete BEFORE DELETE ON pursuit_events
BEGIN SELECT RAISE(ABORT, 'pursuit_events are append-only'); END;

-- Every tracked opportunity becomes a pursuit with the same id, its notice linked, and its
-- events copied verbatim. The legacy tables stay, frozen, and v_pipeline keeps reading them.
INSERT INTO pursuits (pursuit_id, user_id, title, office_entity_id, office_code, naics_code,
                      stage, pwin, notes, outcome, closed_at, created_at, updated_at)
SELECT t.tracked_id, t.user_id, n.title, n.agency_entity_id,
       CASE WHEN n.full_parent_path_code IS NULL THEN NULL ELSE substr(
           n.full_parent_path_code,
           length(rtrim(n.full_parent_path_code, replace(n.full_parent_path_code, '.', ''))) + 1
       ) END,
       n.naics_code,
       CASE t.stage WHEN 'watching' THEN 'identify' WHEN 'pursuing' THEN 'qualify'
                    WHEN 'bid' THEN 'proposal' WHEN 'submitted' THEN 'submitted'
                    WHEN 'lost' THEN 'submitted' WHEN 'won' THEN 'post-award'
                    ELSE 'identify' END,
       t.pwin, t.notes,
       CASE t.stage WHEN 'won' THEN 'won' WHEN 'lost' THEN 'lost' WHEN 'no-bid' THEN 'no-bid' END,
       CASE WHEN t.stage IN ('lost', 'no-bid') THEN t.updated_at END,
       t.created_at, t.updated_at
FROM tracked_opportunities AS t JOIN notices AS n USING (notice_id);

INSERT INTO pursuit_notices (pursuit_id, user_id, notice_id, role, linked_at)
SELECT t.tracked_id, t.user_id, t.notice_id,
       CASE n.notice_type
           WHEN 'Sources Sought' THEN 'sources-sought'
           WHEN 'Presolicitation' THEN 'presolicitation'
           WHEN 'Award Notice' THEN 'award'
           WHEN 'Modification/Amendment/Cancel' THEN 'amendment'
           WHEN 'Solicitation' THEN 'solicitation'
           WHEN 'Combined Synopsis/Solicitation' THEN 'solicitation'
           ELSE 'other' END,
       t.created_at
FROM tracked_opportunities AS t JOIN notices AS n USING (notice_id);

INSERT INTO pursuit_events (pursuit_id, user_id, changed_at, field, old_value, new_value)
SELECT tracked_id, user_id, changed_at, field, old_value, new_value
FROM tracked_opportunity_events ORDER BY event_id;

-- Views (interface version 1, added): the live pipeline. v_pipeline is superseded, not changed.
CREATE VIEW v_pursuits AS
SELECT p.user_id, p.pursuit_id, p.title, p.summary, p.stage, p.pwin, p.notes, p.held_until,
       p.outcome, p.closed_at, p.office_entity_id, o.name AS office, p.office_code, p.naics_code,
       p.incumbent_contract_id, coalesce(v.name, c.recipient_name) AS incumbent,
       c.pop_end AS incumbent_pop_end,
       (SELECT count(*) FROM pursuit_notices AS pn WHERE pn.pursuit_id = p.pursuit_id) AS notices,
       (SELECT count(*) FROM pursuit_tasks AS t
         WHERE t.pursuit_id = p.pursuit_id AND t.done_at IS NULL) AS open_tasks,
       (SELECT min(t.due) FROM pursuit_tasks AS t
         WHERE t.pursuit_id = p.pursuit_id AND t.done_at IS NULL AND t.due IS NOT NULL) AS next_due,
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
SELECT t.user_id, t.task_id, t.pursuit_id, p.title AS pursuit_title, p.stage AS pursuit_stage,
       p.outcome, p.closed_at, t.stage, t.title, t.origin, t.due, t.done_at, t.created_at
FROM pursuit_tasks AS t JOIN pursuits AS p USING (pursuit_id);
