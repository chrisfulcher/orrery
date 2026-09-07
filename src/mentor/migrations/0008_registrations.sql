-- 0008_registrations: the SAM.gov entity registrations source, the append-only table that
-- keeps each registration record as observed, and v_contractors. Mirrors docs/DESIGN.md §8.
-- Applied inside one transaction by mentor.db.migrate; do not add BEGIN/COMMIT.

INSERT INTO sources (source_id, name, adapter_version, terms) VALUES
    ('sam_entities', 'SAM.gov entity registrations (public extract and Entity Management API v3)',
     '0.0.1', 'U.S. Government work, public domain; accessed with the user''s own SAM.gov API key');

-- One row per registration record as observed (a new row only when the record changed):
-- the public record with every point-of-contact field removed, the typed attributes going
-- to facts. The append-only counterpart of notice_versions for contractors.
CREATE TABLE entity_registrations (
    registration_id INTEGER PRIMARY KEY,
    entity_id       INTEGER NOT NULL REFERENCES entities(entity_id),
    source_id       TEXT NOT NULL REFERENCES sources(source_id),
    source_ref      TEXT NOT NULL,   -- the extract file name, or 'api:v3'
    observed_at     TEXT NOT NULL,
    raw_hash        TEXT NOT NULL,   -- sha256 of raw_json
    raw_json        TEXT NOT NULL
);
CREATE INDEX entity_registrations_entity ON entity_registrations(entity_id, observed_at);

CREATE VIEW v_contractors AS
SELECT e.entity_id, e.name, e.uei, e.cage, e.first_seen_at, e.last_seen_at,
       (SELECT count(*) FROM contracts AS c WHERE c.vendor_entity_id = e.entity_id) AS awards,
       (SELECT coalesce(sum(c.value_usd), 0) FROM contracts AS c
         WHERE c.vendor_entity_id = e.entity_id) AS awards_value_usd,
       (SELECT max(c.last_action_date) FROM contracts AS c
         WHERE c.vendor_entity_id = e.entity_id) AS last_award_date,
       (SELECT f.value FROM facts AS f
         WHERE f.subject_type = 'entity' AND f.subject_id = CAST(e.entity_id AS TEXT)
           AND f.predicate = 'sam.registration_status'
         ORDER BY f.observed_at DESC, f.fact_id DESC LIMIT 1) AS registration_status,
       (SELECT f.value FROM facts AS f
         WHERE f.subject_type = 'entity' AND f.subject_id = CAST(e.entity_id AS TEXT)
           AND f.predicate = 'sam.registration_expires'
         ORDER BY f.observed_at DESC, f.fact_id DESC LIMIT 1) AS registration_expires,
       (SELECT f.value FROM facts AS f
         WHERE f.subject_type = 'entity' AND f.subject_id = CAST(e.entity_id AS TEXT)
           AND f.predicate = 'sam.naics_primary'
         ORDER BY f.observed_at DESC, f.fact_id DESC LIMIT 1) AS naics_primary
FROM entities AS e WHERE e.kind = 'contractor';
