-- 0001_initial: identity/provenance and ingestion layers, search, users.
-- Mirrors docs/DESIGN.md §8. Applied inside one transaction by orrery.db.migrate;
-- do not add BEGIN/COMMIT. Timestamps are TEXT, ISO-8601 UTC, 'YYYY-MM-DDTHH:MM:SSZ'.

-- Identity and provenance layer (public data) ---------------------------------

CREATE TABLE sources (
    source_id       TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    terms           TEXT NOT NULL,
    last_run_at     TEXT
);

CREATE TABLE entities (
    entity_id        INTEGER PRIMARY KEY,
    kind             TEXT NOT NULL CHECK (kind IN ('agency', 'office', 'contractor', 'place')),
    name             TEXT NOT NULL,
    agency_path_code TEXT UNIQUE,   -- SQLite UNIQUE treats NULLs as distinct
    uei              TEXT UNIQUE,
    cage             TEXT UNIQUE,
    parent_entity_id INTEGER REFERENCES entities(entity_id),
    source_id        TEXT NOT NULL REFERENCES sources(source_id),
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL
);
CREATE INDEX entities_parent ON entities(parent_entity_id);

CREATE TABLE entity_aliases (
    alias_id    INTEGER PRIMARY KEY,
    alias       TEXT NOT NULL,
    entity_id   INTEGER REFERENCES entities(entity_id),   -- NULL while unresolved
    source_id   TEXT NOT NULL REFERENCES sources(source_id),
    method      TEXT CHECK (method IN ('exact_key', 'manual', 'fuzzy', 'llm')),
    confidence  REAL CHECK (confidence BETWEEN 0 AND 1),
    resolved_at TEXT,
    -- Unresolved rows have entity_id, method, and resolved_at all null; resolved rows have all set.
    CHECK ((entity_id IS NULL) = (method IS NULL) AND (entity_id IS NULL) = (resolved_at IS NULL))
);
CREATE INDEX entity_aliases_alias  ON entity_aliases(alias);
CREATE INDEX entity_aliases_entity ON entity_aliases(entity_id);

CREATE TABLE people (
    person_id     INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    role_title    TEXT,
    entity_id     INTEGER REFERENCES entities(entity_id),
    source_id     TEXT NOT NULL REFERENCES sources(source_id),
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);
CREATE INDEX people_entity ON people(entity_id);

CREATE TABLE contracts (
    contract_id        INTEGER PRIMARY KEY,
    piid               TEXT NOT NULL,   -- unique per awarding agency upstream, so indexed not unique
    awarding_entity_id INTEGER REFERENCES entities(entity_id),
    vendor_entity_id   INTEGER REFERENCES entities(entity_id),
    value_usd          REAL,
    pop_start          TEXT,
    pop_end            TEXT,
    source_id          TEXT NOT NULL REFERENCES sources(source_id)
);
CREATE INDEX contracts_piid ON contracts(piid);

CREATE TABLE facts (
    fact_id           INTEGER PRIMARY KEY,
    subject_type      TEXT NOT NULL CHECK (subject_type IN ('entity', 'person', 'contract', 'notice')),
    subject_id        TEXT NOT NULL,
    predicate         TEXT NOT NULL,
    value_type        TEXT NOT NULL CHECK (value_type IN ('text', 'number', 'date', 'ref')),
    value             TEXT NOT NULL,
    source_id         TEXT NOT NULL REFERENCES sources(source_id),
    source_ref        TEXT,
    observed_at       TEXT NOT NULL,
    confidence        REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    extraction_method TEXT NOT NULL,
    model             TEXT
);
CREATE INDEX facts_subject ON facts(subject_type, subject_id);
-- Facts are append-only: a correction is a new row with a later observed_at.
CREATE TRIGGER facts_append_only_update BEFORE UPDATE ON facts
BEGIN SELECT RAISE(ABORT, 'facts are append-only'); END;
CREATE TRIGGER facts_append_only_delete BEFORE DELETE ON facts
BEGIN SELECT RAISE(ABORT, 'facts are append-only'); END;

-- Ingestion layer (public data) ------------------------------------------------

CREATE TABLE notices (
    id                    INTEGER PRIMARY KEY,   -- stable integer key for FTS5 / vector tables
    notice_id             TEXT NOT NULL UNIQUE,  -- SAM.gov natural key; FK target
    solicitation_number   TEXT,
    title                 TEXT NOT NULL,
    notice_type           TEXT,
    full_parent_path_name TEXT,
    full_parent_path_code TEXT,
    agency_entity_id      INTEGER REFERENCES entities(entity_id),
    naics_code            TEXT,
    psc_code              TEXT,
    set_aside_code        TEXT,
    posted_at             TEXT,
    response_deadline     TEXT,
    place_of_performance  TEXT,   -- JSON text of the API object
    active                INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    first_seen_at         TEXT NOT NULL,
    last_seen_at          TEXT NOT NULL,
    source_id             TEXT NOT NULL REFERENCES sources(source_id),
    description           TEXT,   -- NULL until fetched (the v2 API returns a URL)
    raw_json              TEXT NOT NULL
);
CREATE INDEX notices_posted_at  ON notices(posted_at);
CREATE INDEX notices_naics_code ON notices(naics_code);
CREATE INDEX notices_agency     ON notices(agency_entity_id);

CREATE TABLE notice_versions (
    version_id  INTEGER PRIMARY KEY,
    notice_id   TEXT NOT NULL REFERENCES notices(notice_id),
    observed_at TEXT NOT NULL,
    raw_hash    TEXT NOT NULL,   -- sha256 of raw_json; a change is detected when this differs
    raw_json    TEXT NOT NULL
);
CREATE INDEX notice_versions_notice ON notice_versions(notice_id, observed_at);

CREATE TABLE ingestion_runs (
    run_id           INTEGER PRIMARY KEY,
    source_id        TEXT NOT NULL REFERENCES sources(source_id),
    posted_from      TEXT,
    posted_to        TEXT,
    started_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    finished_at      TEXT,
    records_returned INTEGER,
    requests_spent   INTEGER NOT NULL DEFAULT 0,
    quota_remaining  INTEGER,
    status           TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'succeeded', 'failed')),
    error            TEXT,
    cursor           TEXT
);

CREATE TABLE api_requests (
    request_id     INTEGER PRIMARY KEY,
    run_id         INTEGER NOT NULL REFERENCES ingestion_runs(run_id),
    endpoint       TEXT NOT NULL,   -- URL with the api_key parameter stripped; never the key
    notice_id      TEXT REFERENCES notices(notice_id),
    status_code    INTEGER,         -- NULL when no HTTP response was received
    response_bytes INTEGER,
    requested_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    error          TEXT
);
CREATE INDEX api_requests_requested_at ON api_requests(requested_at);
CREATE INDEX api_requests_run          ON api_requests(run_id);

CREATE TABLE attachments (
    attachment_id    INTEGER PRIMARY KEY,
    notice_id        TEXT NOT NULL REFERENCES notices(notice_id),
    url              TEXT NOT NULL,
    filename         TEXT,           -- learned at fetch time (resourceLinks URLs are opaque)
    fetch_status     TEXT NOT NULL DEFAULT 'pending'
                     CHECK (fetch_status IN ('pending', 'fetched', 'failed', 'skipped')),
    priority         INTEGER NOT NULL DEFAULT 0,
    fetched_at       TEXT,
    ingestion_run_id INTEGER REFERENCES ingestion_runs(run_id),   -- the run that fetched it
    path             TEXT,
    content_hash     TEXT,
    extracted_text   TEXT,
    UNIQUE (notice_id, url)
);
CREATE INDEX attachments_queue ON attachments(fetch_status, priority);

CREATE TABLE naics_codes (code TEXT PRIMARY KEY, title TEXT NOT NULL);
CREATE TABLE psc_codes   (code TEXT PRIMARY KEY, title TEXT NOT NULL);

-- Search layer (derived, rebuildable: INSERT INTO notices_fts(notices_fts) VALUES ('rebuild')) --

CREATE VIRTUAL TABLE notices_fts USING fts5(
    title, description,
    content='notices', content_rowid='id'
);
CREATE TRIGGER notices_fts_ai AFTER INSERT ON notices BEGIN
    INSERT INTO notices_fts(rowid, title, description) VALUES (new.id, new.title, new.description);
END;
CREATE TRIGGER notices_fts_ad AFTER DELETE ON notices BEGIN
    INSERT INTO notices_fts(notices_fts, rowid, title, description)
    VALUES ('delete', old.id, old.title, old.description);
END;
CREATE TRIGGER notices_fts_au AFTER UPDATE ON notices BEGIN
    INSERT INTO notices_fts(notices_fts, rowid, title, description)
    VALUES ('delete', old.id, old.title, old.description);
    INSERT INTO notices_fts(rowid, title, description) VALUES (new.id, new.title, new.description);
END;

-- Workspace layer (private; every row carries user_id) -------------------------

CREATE TABLE users (
    user_id    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Seed rows -------------------------------------------------------------------

INSERT INTO sources (source_id, name, adapter_version, terms) VALUES
    ('sam_opportunities_api', 'SAM.gov Get Opportunities API v2', '0.0.1',
     'U.S. Government work, public domain; accessed with the user''s own SAM.gov API key'),
    ('sam_bulk_csv', 'SAM.gov Data Services contract opportunity extracts', '0.0.1',
     'U.S. Government work, public domain; no key or quota');
INSERT INTO users (user_id, name) VALUES (1, 'local');
