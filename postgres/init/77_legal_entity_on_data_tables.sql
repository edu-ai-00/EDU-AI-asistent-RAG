-- last change in code by Martin Safranek (2026-08-02).
-- 77_legal_entity_on_data_tables.sql
-- legal_entity_code on every document and QA table, plus the logs.
--
-- "legal entity", never "school": the same platform is meant to serve companies,
-- and a column called school_id would have to be renamed the first time it does.
-- The term is in the table name (edu_ai.legal_entity, migration 76) and in every
-- column added here.
--
-- ── nullable for now, and that is deliberate ────────────────────────────────
-- The correct end state is NOT NULL. It is not set here because nothing writes the
-- column yet: /v1/ingest does not take the entity, and none of the six flows carry
-- it. NOT NULL today would mean the next ingest fails, and a DEFAULT would be
-- worse — it would file every document under one entity, silently and wrongly,
-- which is precisely the failure multi-tenancy exists to prevent.
--
-- So the sequence is: column now (ingest keeps working, the gap is visible), API
-- and flows next, NOT NULL last. Rows written in between are findable:
--
--     SELECT count(*) FROM edu_ai.edu_ai_document_chunks
--      WHERE legal_entity_code IS NULL;
--
-- The tables are empty as this is written, so the window can stay short.
--
-- ── the logs are included ───────────────────────────────────────────────────
-- They were not named in the request, which said documents and QA. But a query log
-- without the entity cannot answer "what are this entity's users asking", which is
-- the only reason it exists, and adding it later means a second pass over the same
-- tables. No foreign key on the logs: they are append-only, written after the
-- answer is sent, and must survive the entity being removed.
--
-- ── the one unique index that breaks ────────────────────────────────────────
-- qa_pack_manifest_one_active_per_pack is UNIQUE (pack_id) WHERE is_active, and
-- pack_id comes from the user (user_defined_pack_id on /v1/ingest). Two entities
-- cannot both have an active pack called "faq". It becomes composite here.
-- NULLS NOT DISTINCT (Postgres 15+, this is 16.3) so that during the nullable
-- window two entity-less packs still collide rather than both being accepted.
--
-- Idempotent; transactional.

BEGIN;

-- ── documents ───────────────────────────────────────────────────────────────
ALTER TABLE edu_ai.edu_ai_document_metadata
    ADD COLUMN IF NOT EXISTS legal_entity_code text
        REFERENCES edu_ai.legal_entity (legal_entity_code);
ALTER TABLE edu_ai.edu_ai_document_chunks
    ADD COLUMN IF NOT EXISTS legal_entity_code text
        REFERENCES edu_ai.legal_entity (legal_entity_code);
ALTER TABLE edu_ai.edu_ai_document_elements
    ADD COLUMN IF NOT EXISTS legal_entity_code text
        REFERENCES edu_ai.legal_entity (legal_entity_code);
ALTER TABLE edu_ai.edu_ai_document_tables
    ADD COLUMN IF NOT EXISTS legal_entity_code text
        REFERENCES edu_ai.legal_entity (legal_entity_code);
ALTER TABLE edu_ai.edu_ai_chunk_keywords
    ADD COLUMN IF NOT EXISTS legal_entity_code text
        REFERENCES edu_ai.legal_entity (legal_entity_code);

-- ── QA ──────────────────────────────────────────────────────────────────────
ALTER TABLE edu_ai.qa_entries
    ADD COLUMN IF NOT EXISTS legal_entity_code text
        REFERENCES edu_ai.legal_entity (legal_entity_code);
ALTER TABLE edu_ai.qa_variants
    ADD COLUMN IF NOT EXISTS legal_entity_code text
        REFERENCES edu_ai.legal_entity (legal_entity_code);
ALTER TABLE edu_ai.qa_embedding
    ADD COLUMN IF NOT EXISTS legal_entity_code text
        REFERENCES edu_ai.legal_entity (legal_entity_code);
ALTER TABLE edu_ai.qa_pack_manifest
    ADD COLUMN IF NOT EXISTS legal_entity_code text
        REFERENCES edu_ai.legal_entity (legal_entity_code);

-- ── logs: column only, no foreign key (see the header) ──────────────────────
ALTER TABLE edu_ai.ingestion_log
    ADD COLUMN IF NOT EXISTS legal_entity_code text;
ALTER TABLE edu_ai.qa_query_log
    ADD COLUMN IF NOT EXISTS legal_entity_code text;
ALTER TABLE edu_ai.qa_feedback
    ADD COLUMN IF NOT EXISTS legal_entity_code text;

-- ── indexes ─────────────────────────────────────────────────────────────────
-- Every retrieval query will filter on this column first, so it leads the index.
-- The chunk and keyword indexes are the ones that matter for search; the rest
-- serve administration and the foreign keys.
CREATE INDEX IF NOT EXISTS chunks_legal_entity_idx
    ON edu_ai.edu_ai_document_chunks (legal_entity_code);
CREATE INDEX IF NOT EXISTS keywords_legal_entity_idx
    ON edu_ai.edu_ai_chunk_keywords (legal_entity_code);
CREATE INDEX IF NOT EXISTS metadata_legal_entity_idx
    ON edu_ai.edu_ai_document_metadata (legal_entity_code);
CREATE INDEX IF NOT EXISTS elements_legal_entity_idx
    ON edu_ai.edu_ai_document_elements (legal_entity_code);
CREATE INDEX IF NOT EXISTS tables_legal_entity_idx
    ON edu_ai.edu_ai_document_tables (legal_entity_code);
CREATE INDEX IF NOT EXISTS qa_entries_legal_entity_idx
    ON edu_ai.qa_entries (legal_entity_code);
CREATE INDEX IF NOT EXISTS qa_variants_legal_entity_idx
    ON edu_ai.qa_variants (legal_entity_code);
CREATE INDEX IF NOT EXISTS qa_embedding_legal_entity_idx
    ON edu_ai.qa_embedding (legal_entity_code);
CREATE INDEX IF NOT EXISTS qa_query_log_legal_entity_idx
    ON edu_ai.qa_query_log (legal_entity_code, created_at DESC);
CREATE INDEX IF NOT EXISTS ingestion_log_legal_entity_idx
    ON edu_ai.ingestion_log (legal_entity_code);

-- ── the pack id is only unique within an entity ─────────────────────────────
DROP INDEX IF EXISTS edu_ai.qa_pack_manifest_one_active_per_pack;
CREATE UNIQUE INDEX IF NOT EXISTS qa_pack_manifest_one_active_per_pack
    ON edu_ai.qa_pack_manifest (legal_entity_code, pack_id)
    NULLS NOT DISTINCT WHERE is_active;

COMMIT;
