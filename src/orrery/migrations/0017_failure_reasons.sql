-- 0017_failure_reasons: why a fetch failed, not only that it did. The three fetch stages
-- recorded a terminal 'failed' and discarded the exception, so a notice whose description
-- SAM.gov answered with a 500 was indistinguishable from one whose attachment is gone for
-- good, and from one the network never reached. Those want different answers: 404 is
-- permanent, 429 and 5xx are worth another try, a shape change is a defect here. Each stage
-- gets a kind, classified where the error is raised, and the already key-redacted message
-- beside it. Recording them now means a retry policy can act on history already collected
-- rather than needing a backfill of rows that never said why.
-- Mirrors docs/DESIGN.md §8. Applied inside one transaction by orrery.db.migrate.

ALTER TABLE notices ADD COLUMN description_failure_kind TEXT;
ALTER TABLE notices ADD COLUMN description_failure_detail TEXT;
ALTER TABLE notices ADD COLUMN manifest_failure_kind TEXT;
ALTER TABLE notices ADD COLUMN manifest_failure_detail TEXT;
ALTER TABLE attachments ADD COLUMN failure_kind TEXT;
ALTER TABLE attachments ADD COLUMN failure_detail TEXT;
