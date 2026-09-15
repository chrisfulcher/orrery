-- 0020_exclusions: SAM.gov exclusions (debarments and suspensions) as facts on contractors,
-- and one view that reads them back.
--
-- The source is the daily public exclusions extract (§5 source 3), which needs no key and no
-- quota. It holds active records only, so an exclusion that ends simply stops appearing:
-- absence from a later complete file is itself an observation, and becomes a 'terminated'
-- status fact at that file's cut rather than a deletion. Nothing here is a column on
-- entities for the same reason registration attributes are not (§8 facts): an exclusion is
-- something a source said on a day, it is corrected by a later observation, and the store
-- keeps every version.
--
-- v_exclusions folds the facts back into one row per (entity, SAM Number): the latest value
-- of each predicate, and a derived 'current' so no reader has to know that "excluded today"
-- means an active status with a termination date that has not passed. Views only ever gain
-- columns (§8), and this is a new one.
--
-- Mirrors docs/DESIGN.md §8. Applied inside one transaction by orrery.db.migrate.

INSERT INTO sources (source_id, name, adapter_version, terms) VALUES
    ('sam_exclusions', 'SAM.gov exclusions (public daily extract, Public V2)', '0.0.1',
     'U.S. Government work, public domain; no key or quota');

CREATE VIEW v_exclusions AS
WITH latest AS (
    SELECT f.subject_id AS subject_id, f.source_ref AS sam_number, f.predicate AS predicate,
           f.value AS value, f.observed_at AS observed_at,
           row_number() OVER (
               PARTITION BY f.subject_id, f.source_ref, f.predicate
               ORDER BY f.observed_at DESC, f.fact_id DESC
           ) AS rn
    FROM facts AS f
    WHERE f.subject_type = 'entity' AND f.source_id = 'sam_exclusions'
      AND f.predicate LIKE 'sam.exclusion.%' AND f.source_ref IS NOT NULL
),
pivoted AS (
    SELECT subject_id, sam_number,
           max(CASE WHEN predicate = 'sam.exclusion.status' THEN value END) AS status,
           max(CASE WHEN predicate = 'sam.exclusion.type' THEN value END) AS exclusion_type,
           max(CASE WHEN predicate = 'sam.exclusion.program' THEN value END) AS program,
           max(CASE WHEN predicate = 'sam.exclusion.agency' THEN value END) AS agency,
           max(CASE WHEN predicate = 'sam.exclusion.active_date' THEN value END) AS active_date,
           max(CASE WHEN predicate = 'sam.exclusion.termination_date' THEN value END)
               AS termination_date,
           max(observed_at) AS observed_at
    FROM latest WHERE rn = 1
    GROUP BY subject_id, sam_number
)
SELECT e.entity_id AS entity_id, e.name AS name, e.uei AS uei, e.cage AS cage,
       p.sam_number AS sam_number, p.status AS status, p.exclusion_type AS exclusion_type,
       p.program AS program, p.agency AS agency, p.active_date AS active_date,
       p.termination_date AS termination_date, p.observed_at AS observed_at,
       CASE WHEN p.status = 'active'
                 AND (p.termination_date IS NULL OR p.termination_date >= date('now'))
            THEN 1 ELSE 0 END AS current
FROM pivoted AS p
JOIN entities AS e ON e.entity_id = CAST(p.subject_id AS INTEGER);
