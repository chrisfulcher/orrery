-- 0019_reference: the NAICS and PSC code lists as seed data. naics_codes and psc_codes have
-- been empty since 0001, so every screen that shows a notice shows 541512 and D302 and leaves
-- the reader to look them up elsewhere, which an offline, auditable install cannot ask of
-- anyone. The lists ship inside the package (src/orrery/reference/) and orrery.reference.seed
-- loads them at the end of every db.migrate.
--
-- They are ingested data like any other, so they carry provenance: a sources row each, and a
-- source_id on every code. The column is nullable because the tables may already hold rows a
-- user loaded by hand before this migration, and those rows are not the Census's or GSA's to
-- claim.
--
-- Mirrors docs/DESIGN.md §8. Applied inside one transaction by orrery.db.migrate.

ALTER TABLE naics_codes ADD COLUMN source_id TEXT REFERENCES sources(source_id);
ALTER TABLE psc_codes   ADD COLUMN source_id TEXT REFERENCES sources(source_id);

INSERT INTO sources (source_id, name, adapter_version, terms) VALUES
    ('census_naics', 'NAICS 2022, U.S. Census Bureau', '2022',
     'U.S. Government work, public domain; shipped with orrery'),
    ('gsa_psc_manual', 'Product and Service Codes Manual, GSA', '2025-04',
     'U.S. Government work, public domain; shipped with orrery');

-- v_notices gains the two titles. Both joins are LEFT: a notice carries its codes as plain
-- text and neither table is a foreign-key target, so an unknown or retired code still reads.
-- Every 0015 column is kept in order.
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
       s.summary, s.work_type, s.keywords, s.stated_set_aside, s.model AS summary_model,
       n.manifest_status, n.manifest_checked_at,
       nc.title AS naics_title, pc.title AS psc_title
FROM notices AS n
LEFT JOIN entities AS e ON e.entity_id = n.agency_entity_id
LEFT JOIN v_notice_summaries AS s ON s.notice_id = n.notice_id
LEFT JOIN naics_codes AS nc ON nc.code = n.naics_code
LEFT JOIN psc_codes AS pc ON pc.code = n.psc_code;
