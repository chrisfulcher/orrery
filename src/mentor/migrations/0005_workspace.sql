-- 0005_workspace: saved searches, the opportunity pipeline, and the company profile.
-- Workspace layer: private, every row carries user_id (v1 is single-user: user 1, 'local').
-- Mirrors docs/DESIGN.md §8. Applied inside one transaction by mentor.db.migrate.

CREATE TABLE saved_searches (
    search_id            INTEGER PRIMARY KEY,
    user_id              INTEGER NOT NULL REFERENCES users(user_id),
    name                 TEXT NOT NULL,
    query                TEXT,     -- FTS5 text; NULL means filters only
    naics                TEXT CHECK (naics IS NULL OR json_type(naics) = 'array'),
    set_asides           TEXT CHECK (set_asides IS NULL OR json_type(set_asides) = 'array'),
    agency_path_prefixes TEXT CHECK (agency_path_prefixes IS NULL
                                     OR json_type(agency_path_prefixes) = 'array'),
    deadline_within_days INTEGER CHECK (deadline_within_days > 0),
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (user_id, name)
);

CREATE TABLE tracked_opportunities (
    tracked_id INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(user_id),
    notice_id  TEXT NOT NULL REFERENCES notices(notice_id),
    stage      TEXT NOT NULL CHECK (stage IN
                   ('watching', 'pursuing', 'bid', 'no-bid', 'submitted', 'won', 'lost')),
    pwin       INTEGER CHECK (pwin BETWEEN 0 AND 100),
    notes      TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (user_id, notice_id)
);

-- Append-only: PWin and stage trajectory are intelligence, like notice_versions.
CREATE TABLE tracked_opportunity_events (
    event_id   INTEGER PRIMARY KEY,
    tracked_id INTEGER NOT NULL REFERENCES tracked_opportunities(tracked_id),
    user_id    INTEGER NOT NULL REFERENCES users(user_id),
    changed_at TEXT NOT NULL,
    field      TEXT NOT NULL CHECK (field IN ('stage', 'pwin', 'notes')),
    old_value  TEXT,          -- NULL on first sight
    new_value  TEXT
);
CREATE TRIGGER tracked_events_append_only_update BEFORE UPDATE ON tracked_opportunity_events
BEGIN SELECT RAISE(ABORT, 'tracked_opportunity_events are append-only'); END;
CREATE TRIGGER tracked_events_append_only_delete BEFORE DELETE ON tracked_opportunity_events
BEGIN SELECT RAISE(ABORT, 'tracked_opportunity_events are append-only'); END;

CREATE TABLE company_profiles (
    user_id                INTEGER PRIMARY KEY REFERENCES users(user_id),   -- one per user
    name                   TEXT,
    uei                    TEXT,     -- links to the company's own entity row once source 3 lands
    cage                   TEXT,
    naics                  TEXT CHECK (naics IS NULL OR json_type(naics) = 'array'),
    certifications         TEXT CHECK (certifications IS NULL
                                       OR json_type(certifications) = 'array'),
    capability_statement   TEXT,
    target_agency_prefixes TEXT CHECK (target_agency_prefixes IS NULL
                                       OR json_type(target_agency_prefixes) = 'array'),
    updated_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Fetch priority is computed at queue time from saved searches (fetch/queue.py); the column
-- nothing ever wrote goes away, and the queue index keeps only the status it filters on.
DROP INDEX attachments_queue;
ALTER TABLE attachments DROP COLUMN priority;
CREATE INDEX attachments_queue ON attachments(fetch_status);
