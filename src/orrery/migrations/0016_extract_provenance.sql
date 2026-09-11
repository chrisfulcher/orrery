-- 0016_extract_provenance: when the file a run read was generated, beside when the run ran.
-- The bulk extract is a snapshot cut at some point before the run that reads it, and the
-- difference matters: a notice the keyed API ingested after the cut is legitimately absent
-- from the file, and a pass that mistakes the run's own start for the file's cut reads that
-- absence as evidence the notice is gone. source_generated_at records the cut as the source
-- stated it, so the active pass has something true to compare against and a run that could
-- not establish it says so rather than guessing.
-- Mirrors docs/DESIGN.md §8. Applied inside one transaction by orrery.db.migrate.

ALTER TABLE ingestion_runs ADD COLUMN source_generated_at TEXT;
