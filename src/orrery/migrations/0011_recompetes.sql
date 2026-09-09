-- 0011_recompetes: the potential end of an award's period of performance (options included),
-- the date a recompete radar runs on. Backfilled from the stored source row. Mirrors
-- docs/DESIGN.md §8. Applied inside one transaction by orrery.db.migrate; do not add
-- BEGIN/COMMIT.

ALTER TABLE contracts ADD COLUMN pop_potential_end TEXT;
UPDATE contracts SET pop_potential_end = nullif(
    substr(json_extract(raw_json, '$.period_of_performance_potential_end_date'), 1, 10), ''
);
CREATE INDEX contracts_pop_potential_end ON contracts(pop_potential_end);

-- v_contracts keeps every 0007 column and gains pop_potential_end (interface version 1).
DROP VIEW v_contracts;
CREATE VIEW v_contracts AS
SELECT c.contract_id, c.award_key, c.piid, c.parent_piid,
       c.awarding_entity_id, c.awarding_office_code,
       coalesce(o.name, c.awarding_office_name) AS awarding_office,
       c.vendor_entity_id, coalesce(v.name, c.recipient_name) AS vendor,
       coalesce(v.uei, c.recipient_uei) AS vendor_uei, c.cage,
       c.solicitation_identifier, c.award_date, c.last_action_date, c.pop_start, c.pop_end,
       c.value_usd, c.potential_value_usd, c.naics_code, c.psc_code, c.award_type_code,
       c.set_aside_code, c.extent_competed_code, c.source_id, c.first_seen_at, c.last_seen_at,
       json_extract(c.raw_json, '$.usaspending_permalink') AS url,
       c.pop_potential_end
FROM contracts AS c
LEFT JOIN entities AS o ON o.entity_id = c.awarding_entity_id
LEFT JOIN entities AS v ON v.entity_id = c.vendor_entity_id;
