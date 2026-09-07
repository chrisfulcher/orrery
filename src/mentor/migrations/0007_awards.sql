-- 0007_awards: contracts in their award shape, the USAspending source, v_contracts, and the
-- award columns of v_notices. Mirrors docs/DESIGN.md §8. Applied inside one transaction by
-- mentor.db.migrate; do not add BEGIN/COMMIT.

-- contracts was created by 0001 and no adapter has ever written it, so it is recreated in
-- the shape the awards adapter needs rather than altered column by column.
DROP TABLE contracts;
CREATE TABLE contracts (
    contract_id             INTEGER PRIMARY KEY,
    award_key               TEXT NOT NULL UNIQUE,  -- USAspending contract_award_unique_key
    piid                    TEXT NOT NULL,         -- unique per awarding agency upstream: indexed, not unique
    parent_piid             TEXT,
    awarding_entity_id      INTEGER REFERENCES entities(entity_id),  -- NULL while the office is unresolved
    awarding_office_code    TEXT,                  -- the AAC; the last segment of a SAM.gov agency path
    awarding_office_name    TEXT,
    vendor_entity_id        INTEGER REFERENCES entities(entity_id),  -- NULL while the vendor is unresolved
    recipient_name          TEXT,
    recipient_uei           TEXT,
    cage                    TEXT,
    solicitation_identifier TEXT,
    award_date              TEXT,                  -- base action date
    last_action_date        TEXT,
    pop_start               TEXT,
    pop_end                 TEXT,                  -- current end of the period of performance
    value_usd               REAL,                  -- current total value of the award
    potential_value_usd     REAL,
    naics_code              TEXT,
    psc_code                TEXT,
    award_type_code         TEXT,
    set_aside_code          TEXT,
    extent_competed_code    TEXT,
    source_id               TEXT NOT NULL REFERENCES sources(source_id),
    first_seen_at           TEXT NOT NULL,
    last_seen_at            TEXT NOT NULL,
    raw_json                TEXT NOT NULL          -- the source row minus vendor contact and officer columns
);
CREATE INDEX contracts_piid         ON contracts(piid);
CREATE INDEX contracts_office       ON contracts(awarding_office_code, naics_code, last_action_date);
CREATE INDEX contracts_vendor       ON contracts(vendor_entity_id, last_action_date);
CREATE INDEX contracts_solicitation ON contracts(solicitation_identifier);

INSERT INTO sources (source_id, name, adapter_version, terms) VALUES
    ('usaspending_awards', 'USAspending award summaries (FPDS)', '0.0.1',
     'U.S. Government work, public domain; no key or quota');

CREATE VIEW v_contracts AS
SELECT c.contract_id, c.award_key, c.piid, c.parent_piid,
       c.awarding_entity_id, c.awarding_office_code,
       coalesce(o.name, c.awarding_office_name) AS awarding_office,
       c.vendor_entity_id, coalesce(v.name, c.recipient_name) AS vendor,
       coalesce(v.uei, c.recipient_uei) AS vendor_uei, c.cage,
       c.solicitation_identifier, c.award_date, c.last_action_date, c.pop_start, c.pop_end,
       c.value_usd, c.potential_value_usd, c.naics_code, c.psc_code, c.award_type_code,
       c.set_aside_code, c.extent_competed_code, c.source_id, c.first_seen_at, c.last_seen_at,
       json_extract(c.raw_json, '$.usaspending_permalink') AS url
FROM contracts AS c
LEFT JOIN entities AS o ON o.entity_id = c.awarding_entity_id
LEFT JOIN entities AS v ON v.entity_id = c.vendor_entity_id;

-- v_notices gains the award fields a notice may carry (award notices and justifications),
-- read from raw_json as the source wrote them. Every 0006 column is kept.
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
                nullif(json_extract(n.raw_json, '$.Awardee'), '')) AS awardee
FROM notices AS n LEFT JOIN entities AS e ON e.entity_id = n.agency_entity_id;
