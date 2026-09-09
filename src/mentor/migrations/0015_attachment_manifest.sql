-- 0015_attachment_manifest: attachments discovered without a key. Attachment rows have only
-- ever come from a notice's resourceLinks, which the keyed search carries and the bulk extract
-- does not, so a store backfilled from the free extract ends up with no attachments at all and
-- the design's central claim -- search inside the solicitation documents -- has nothing to
-- search. SAM.gov's web UI reads the same list from an unkeyed endpoint, so discovery becomes
-- free (docs/notes/sam-manifest-probe.md).
--
-- Two per-notice columns record the check, mirroring description_status from 0002:
-- 'checked' means a manifest was read, whether or not it named any files, which is what stops
-- a notice with no attachments from being asked about forever. 'unknown' is SAM.gov answering
-- that it has no such notice, which is terminal; 'failed' is transient and comes round again.
--
-- attachments gains what the manifest supplies: how the row was found, the declared size and
-- type (so an oversized file is skipped without spending a request, and the filename and type
-- are known before the download rather than after), the access flags as stated, the raw item,
-- and when a manifest last named it. A resourceLinks row is a first observation and not the
-- source of record, because amendments add files after a notice is ingested.
-- Mirrors docs/DESIGN.md §8. Applied inside one transaction by mentor.db.migrate.

ALTER TABLE notices ADD COLUMN manifest_status TEXT NOT NULL DEFAULT 'pending'
    CHECK (manifest_status IN ('pending', 'checked', 'unknown', 'failed'));
ALTER TABLE notices ADD COLUMN manifest_checked_at TEXT;

ALTER TABLE attachments ADD COLUMN discovered_by TEXT NOT NULL DEFAULT 'resource_links'
    CHECK (discovered_by IN ('resource_links', 'manifest'));
ALTER TABLE attachments ADD COLUMN declared_size INTEGER;
ALTER TABLE attachments ADD COLUMN mime_type TEXT;
ALTER TABLE attachments ADD COLUMN access_status TEXT;
ALTER TABLE attachments ADD COLUMN export_controlled INTEGER NOT NULL DEFAULT 0;
ALTER TABLE attachments ADD COLUMN deleted_upstream INTEGER NOT NULL DEFAULT 0;
ALTER TABLE attachments ADD COLUMN manifest_json TEXT;
ALTER TABLE attachments ADD COLUMN last_seen_at TEXT;

-- The manifest queue reads these two together, as the description queue reads its status.
CREATE INDEX notices_manifest_queue ON notices(manifest_status, manifest_checked_at);

-- v_notices gains the manifest state. Every 0013 column is kept in order.
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
       n.manifest_status, n.manifest_checked_at
FROM notices AS n
LEFT JOIN entities AS e ON e.entity_id = n.agency_entity_id
LEFT JOIN v_notice_summaries AS s ON s.notice_id = n.notice_id;
