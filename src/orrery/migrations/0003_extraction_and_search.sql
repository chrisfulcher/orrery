-- 0003_extraction_and_search: attachment text extraction state and its FTS5 index.
-- Mirrors docs/DESIGN.md §8. Applied inside one transaction by orrery.db.migrate.

ALTER TABLE attachments ADD COLUMN extract_status TEXT NOT NULL DEFAULT 'pending'
    CHECK (extract_status IN ('pending', 'done', 'unsupported', 'failed'));

-- Derived, rebuildable: INSERT INTO attachments_fts(attachments_fts) VALUES ('rebuild')
CREATE VIRTUAL TABLE attachments_fts USING fts5(
    extracted_text,
    content='attachments', content_rowid='attachment_id'
);
CREATE TRIGGER attachments_fts_ai AFTER INSERT ON attachments BEGIN
    INSERT INTO attachments_fts(rowid, extracted_text)
    VALUES (new.attachment_id, new.extracted_text);
END;
CREATE TRIGGER attachments_fts_ad AFTER DELETE ON attachments BEGIN
    INSERT INTO attachments_fts(attachments_fts, rowid, extracted_text)
    VALUES ('delete', old.attachment_id, old.extracted_text);
END;
CREATE TRIGGER attachments_fts_au AFTER UPDATE ON attachments BEGIN
    INSERT INTO attachments_fts(attachments_fts, rowid, extracted_text)
    VALUES ('delete', old.attachment_id, old.extracted_text);
    INSERT INTO attachments_fts(rowid, extracted_text)
    VALUES (new.attachment_id, new.extracted_text);
END;

-- An external-content table starts empty even when its content table does not. Index the
-- rows that already exist, or the first UPDATE trigger's 'delete' corrupts the index.
INSERT INTO attachments_fts(attachments_fts) VALUES ('rebuild');
