-- last change in code by Martin Safranek (2026-08-02).
-- 81_ingestion_log_user.sql
-- Who ingested it, on the ingestion log.
--
-- The log already gained legal_entity_code (migration 77). Without the user beside
-- it the log answers "an entity ingested something" and not "who did" — and "who
-- uploaded the document that turned out to be wrong" is the question an ingestion
-- log exists to answer.
--
-- No foreign key, for the same reason the query log has none: the log is written
-- during a run that has already partly succeeded, and an unknown user must not
-- turn a completed ingest into a failed insert. It also has to survive the user
-- being deleted.
--
-- Documents and QA carry the same identity as owner_user_id, which is written on
-- every row and not only for private ones — for 'entity' and 'public' it is
-- provenance, and provenance is worth as much as ownership when a document has to
-- be traced back to whoever put it there.
--
-- Idempotent; transactional.

BEGIN;

ALTER TABLE edu_ai.ingestion_log
    ADD COLUMN IF NOT EXISTS user_id text;

COMMENT ON COLUMN edu_ai.ingestion_log.user_id IS
    'The staff user who started this ingest. No FK: the log must not fail on an '
    'unknown user, and must survive the user being deleted.';

CREATE INDEX IF NOT EXISTS ingestion_log_user_idx
    ON edu_ai.ingestion_log (user_id);

COMMENT ON COLUMN edu_ai.edu_ai_document_metadata.owner_user_id IS
    'The staff user who ingested the document. Written on EVERY row: required for '
    'visibility = private, provenance for the rest.';
COMMENT ON COLUMN edu_ai.qa_entries.owner_user_id IS
    'The staff user who imported the entry. Written on EVERY row: required for '
    'visibility = private, provenance for the rest.';

COMMIT;
