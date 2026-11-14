-- 228: document_metadata gets content_sha256, the hash of a version's ingested content.
--
-- The QA portal's Documents page (contracts/qa-portal/CONTRACT.md sec.14.14) skips a
-- re-upload or a web refresh whose content equals the current version. The hash is the
-- SHA-256 of the file's bytes, or of a web page's cleaned text. Approved by the owner on
-- 2026-11-14. Nullable: ingests outside the portal leave it NULL and a NULL never matches.
--
-- Schema change (one nullable column, no default, no index): a clean install gets it from
-- postgres/init_clean and the release manifest. No privilege change: edu_ai_search already
-- has UPDATE on document_metadata (226).
-- Undo: ALTER TABLE edu_ai.document_metadata DROP COLUMN content_sha256.
--
-- Idempotent. No transaction control (runner, from 217).

ALTER TABLE edu_ai.document_metadata ADD COLUMN IF NOT EXISTS content_sha256 text;

DO $check$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'edu_ai'
                      AND table_name = 'document_metadata'
                      AND column_name = 'content_sha256'
                      AND data_type = 'text') THEN
        RAISE EXCEPTION '228: document_metadata.content_sha256 was not created';
    END IF;
END
$check$;
