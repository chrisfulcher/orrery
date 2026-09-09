-- 0014_run_filter: what a run asked for, beside what it returned. A run over a NAICS slice
-- could report a clean success having matched nothing, with no record of the slice it was
-- given or which element of that slice came back empty -- the difference between a quiet
-- market and a filter that no longer selects anything. filter_json holds the slice as sent
-- and the rows each element took. The awards adapter also begins recording its window in
-- posted_from and posted_to, which it left null before; an ingest from a user-supplied file
-- has no window and records none.
-- Mirrors docs/DESIGN.md §8. Applied inside one transaction by mentor.db.migrate.

ALTER TABLE ingestion_runs ADD COLUMN filter_json TEXT;
