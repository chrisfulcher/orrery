-- 0002_description_queue: the v2 search returns a description URL per notice; the text is
-- fetched later by the budgeted queue. Also makes resolved aliases unique per entity.
-- Mirrors docs/DESIGN.md §8. Applied inside one transaction by mentor.db.migrate.

ALTER TABLE notices ADD COLUMN description_url TEXT;
ALTER TABLE notices ADD COLUMN description_status TEXT NOT NULL DEFAULT 'pending'
    CHECK (description_status IN ('pending', 'fetched', 'failed', 'none'));

-- One resolved alias row per (entity, alias). SQLite treats a NULL entity_id as distinct,
-- so unresolved aliases stay unconstrained. Replaces the entity_id-only index.
DROP INDEX entity_aliases_entity;
CREATE UNIQUE INDEX entity_aliases_entity_alias ON entity_aliases(entity_id, alias);
