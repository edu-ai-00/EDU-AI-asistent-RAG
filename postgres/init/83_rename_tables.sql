-- last change in code by Martin Safranek (2026-08-02).
-- 83_rename_tables.sql
-- The table names stop repeating what the schema already says.
--
--   edu_ai.edu_ai_document_metadata  ->  edu_ai.document_metadata
--   edu_ai.edu_ai_document_chunks    ->  edu_ai.document_chunks
--   edu_ai.edu_ai_document_elements  ->  edu_ai.document_elements
--   edu_ai.edu_ai_document_tables    ->  edu_ai.document_tables
--   edu_ai.edu_ai_chunk_keywords     ->  edu_ai.document_chunk_keywords
--   edu_ai.search_setting            ->  edu_ai.settings
--
-- `edu_ai.edu_ai_document_chunks` said "edu_ai" twice; the schema is already
-- edu_ai. And `search_setting` is no longer only about search — since the
-- chunking section landed (migration 45) it also carries the ingest thresholds,
-- so the name was narrower than the content.
--
-- chunk_keywords becomes document_chunk_keywords, not chunk_keywords: everything
-- in this family now shares the document_ prefix, and a bare chunk_keywords would
-- read as if there were chunks that are not a document's.
--
-- Indexes and constraints follow the table automatically but keep their old
-- names, so the ones that spell out the table are renamed too — a schema that is
-- half renamed is worse to read than either name alone.
--
-- embedding_registry.target_table holds a table name AS DATA (rag_vector reads it
-- to know where the vectors live), so it is updated here as well. Missing it would
-- leave vector search pointing at a table that no longer exists.
--
-- Idempotent; transactional.

BEGIN;

ALTER TABLE IF EXISTS edu_ai.edu_ai_document_metadata  RENAME TO document_metadata;
ALTER TABLE IF EXISTS edu_ai.edu_ai_document_chunks    RENAME TO document_chunks;
ALTER TABLE IF EXISTS edu_ai.edu_ai_document_elements  RENAME TO document_elements;
ALTER TABLE IF EXISTS edu_ai.edu_ai_document_tables    RENAME TO document_tables;
ALTER TABLE IF EXISTS edu_ai.edu_ai_chunk_keywords     RENAME TO document_chunk_keywords;
ALTER TABLE IF EXISTS edu_ai.search_setting            RENAME TO settings;

-- indexes and constraints that name the old table
ALTER INDEX IF EXISTS edu_ai.edu_ai_document_metadata_pkey RENAME TO document_metadata_pkey;
ALTER INDEX IF EXISTS edu_ai.edu_ai_document_chunks_pkey   RENAME TO document_chunks_pkey;
ALTER INDEX IF EXISTS edu_ai.edu_ai_document_elements_pkey RENAME TO document_elements_pkey;
ALTER INDEX IF EXISTS edu_ai.edu_ai_document_tables_pkey   RENAME TO document_tables_pkey;
ALTER INDEX IF EXISTS edu_ai.edu_ai_chunk_keywords_pkey    RENAME TO document_chunk_keywords_pkey;
ALTER INDEX IF EXISTS edu_ai.search_setting_pkey           RENAME TO settings_pkey;
ALTER INDEX IF EXISTS edu_ai.search_setting_one_active     RENAME TO settings_one_active;

-- the target table is DATA, not schema: rag_vector reads it from here
UPDATE edu_ai.embedding_registry
   SET target_table = 'edu_ai.document_chunks'
 WHERE target_table = 'edu_ai.edu_ai_document_chunks';

COMMIT;
