-- 0021_hierarchy: the Federal Hierarchy keys on an office entity, and the source they come
-- from. Mirrors docs/DESIGN.md §8. Applied inside one transaction by orrery.db.migrate.
--
-- An office in this store is a segment of a SAM.gov agency path: a code, and whatever name
-- the notice that first created it carried. The Federal Hierarchy API knows the same offices
-- under the same FPDS office code (the AAC, the last segment of the path) and gives each one
-- a stable id of its own. Two columns rather than facts, unusually for an attribute, because
-- both are identity: fh_org_id is the key a later source resolves against and has to be
-- unique, and old_fpds_office_code is the key this one was found by. Everything the lookup
-- says *about* the office -- its type, its agency code, its status, its parent history --
-- stays in facts, where a later observation corrects it without losing the earlier one.
--
-- fh_org_id is unique where present and null elsewhere: an office nobody has looked up yet
-- is not "the same office" as every other one that has not been looked up. The FPDS code is
-- indexed but not unique on purpose: the bulk extract cannot reproduce a deep Defense
-- hierarchy, so one real office can be in the store twice under paths of different lengths,
-- and both rows legitimately carry the same AAC until a fh.same_as fact links them.

INSERT INTO sources (source_id, name, adapter_version, terms) VALUES
    ('sam_federal_hierarchy', 'SAM.gov Federal Hierarchy API (federalorganizations v1)',
     '0.0.1', 'U.S. Government work, public domain; accessed with the user''s own SAM.gov API key');

ALTER TABLE entities ADD COLUMN fh_org_id TEXT;
CREATE UNIQUE INDEX entities_fh_org_id ON entities(fh_org_id) WHERE fh_org_id IS NOT NULL;

ALTER TABLE entities ADD COLUMN old_fpds_office_code TEXT;
CREATE INDEX entities_old_fpds_office_code ON entities(old_fpds_office_code);

-- Views only ever gain columns (§8), so v_entities is restated with the two appended.
DROP VIEW v_entities;
CREATE VIEW v_entities AS
SELECT e.entity_id, e.kind, e.name, e.agency_path_code, e.uei, e.cage,
       e.parent_entity_id, p.name AS parent, e.first_seen_at, e.last_seen_at,
       (SELECT count(*) FROM notices AS n
         WHERE e.agency_path_code IS NOT NULL
           AND (n.full_parent_path_code = e.agency_path_code
                OR n.full_parent_path_code LIKE e.agency_path_code || '.%')) AS notices,
       e.fh_org_id, e.old_fpds_office_code
FROM entities AS e LEFT JOIN entities AS p ON p.entity_id = e.parent_entity_id;
