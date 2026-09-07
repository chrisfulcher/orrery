-- 0006_views: the read-only SQL views, interface version 1. Columns are only ever added; a
-- column that must be renamed or change meaning arrives under a new view name, and the old
-- view stays. Mirrors docs/DESIGN.md §8. Applied inside one transaction by mentor.db.migrate.

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
       (SELECT count(*) FROM notice_versions AS v WHERE v.notice_id = n.notice_id) AS versions
FROM notices AS n LEFT JOIN entities AS e ON e.entity_id = n.agency_entity_id;

CREATE VIEW v_entities AS
SELECT e.entity_id, e.kind, e.name, e.agency_path_code, e.uei, e.cage,
       e.parent_entity_id, p.name AS parent, e.first_seen_at, e.last_seen_at,
       (SELECT count(*) FROM notices AS n
         WHERE e.agency_path_code IS NOT NULL
           AND (n.full_parent_path_code = e.agency_path_code
                OR n.full_parent_path_code LIKE e.agency_path_code || '.%')) AS notices
FROM entities AS e LEFT JOIN entities AS p ON p.entity_id = e.parent_entity_id;

-- Workspace view: private rows; consumers filter by user_id.
CREATE VIEW v_pipeline AS
SELECT t.user_id, t.tracked_id, t.notice_id, t.stage,
       CASE t.stage WHEN 'watching' THEN 0 WHEN 'pursuing' THEN 1 WHEN 'bid' THEN 2
            WHEN 'no-bid' THEN 3 WHEN 'submitted' THEN 4 WHEN 'won' THEN 5 ELSE 6 END
            AS stage_order,
       t.pwin, t.notes, t.created_at, t.updated_at,
       n.title, e.name AS agency, n.response_deadline, n.active
FROM tracked_opportunities AS t
JOIN notices AS n USING (notice_id)
LEFT JOIN entities AS e ON e.entity_id = n.agency_entity_id;

CREATE VIEW v_quota_daily AS
SELECT date(requested_at) AS day, count(*) AS requests,
       sum(status_code IS NULL OR status_code >= 400) AS failed
FROM api_requests GROUP BY day;
