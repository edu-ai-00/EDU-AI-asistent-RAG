-- last change in code by Martin Safranek (2026-07-23).
-- 25_reorder_document_chunks.sql
-- Cosmetic: recreate edu_ai_document_chunks with columns in a logical reading
-- order (identity+provenance → source-doc position → langdetect → chunk content
-- & params → normalized text + tsvectors → embedding → ops), and drop the leftover
-- dropped-column gap. Postgres can't reorder columns in place.
-- Data-preserving (works empty or populated): the 3 GENERATED ts_* columns are
-- excluded from the copy and auto-recompute. Preserves the IDENTITY (canonical
-- sequence name + resumed value), PK, the setting_id FK, and all indexes
-- (HNSW vector + 3 GIN + setting). No inbound FK / view / trigger to preserve.
-- Transactional.

BEGIN;

CREATE TABLE edu_ai.edu_ai_document_chunks_new (
    id                      bigint NOT NULL,
    processing_uuid         uuid NOT NULL,
    file_uuid               uuid NOT NULL,
    file_item_uuid          uuid NOT NULL,
    source_type             text,
    source_name             text,
    file_name               text,
    file_path               text,
    doc_title               text,
    page_number             integer,
    page_element_index      integer,
    vertical_position       double precision,
    header_detected         text,
    txt_structure_type      text,
    txt_should_chunk_reason text,
    output_row_number       integer NOT NULL,
    langdetect_result       text,
    langdetect_result_score double precision,
    chunk_index             integer NOT NULL,
    chunk_text              text NOT NULL,
    chunk_granularity       text DEFAULT 'document'::text,
    chunk_root_id           uuid,
    chunk_parent_id         uuid,
    parent_block_number     integer,
    chunk_size_measured     integer,
    chunk_size_used         integer,
    chunk_overlap_used      integer,
    chunking_mode_used      text,
    chunk_norm_text         text,
    chunk_lemma_text        text,
    chunk_stem_text         text,
    ts_norm  tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, COALESCE(chunk_norm_text, ''::text))) STORED,
    ts_lemma tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, COALESCE(chunk_lemma_text, ''::text))) STORED,
    ts_stem  tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, COALESCE(chunk_stem_text, ''::text))) STORED,
    embedding               edu_ai.vector(1536) NOT NULL,
    embedding_model         text NOT NULL,
    embedding_dimension     integer,
    embedding_provider      text,
    embedding_created_at    timestamp without time zone DEFAULT now(),
    setting_id              uuid,
    ldts_local              timestamp without time zone,
    ldts_gmt                timestamp without time zone
);

-- copy every non-generated column (ts_* recompute automatically)
INSERT INTO edu_ai.edu_ai_document_chunks_new
    (id, processing_uuid, file_uuid, file_item_uuid, source_type, source_name,
     file_name, file_path, doc_title, page_number, page_element_index,
     vertical_position, header_detected, txt_structure_type, txt_should_chunk_reason,
     output_row_number, langdetect_result, langdetect_result_score, chunk_index,
     chunk_text, chunk_granularity, chunk_root_id, chunk_parent_id, parent_block_number,
     chunk_size_measured, chunk_size_used, chunk_overlap_used, chunking_mode_used,
     chunk_norm_text, chunk_lemma_text, chunk_stem_text, embedding, embedding_model,
     embedding_dimension, embedding_provider, embedding_created_at, setting_id,
     ldts_local, ldts_gmt)
SELECT
     id, processing_uuid, file_uuid, file_item_uuid, source_type, source_name,
     file_name, file_path, doc_title, page_number, page_element_index,
     vertical_position, header_detected, txt_structure_type, txt_should_chunk_reason,
     output_row_number, langdetect_result, langdetect_result_score, chunk_index,
     chunk_text, chunk_granularity, chunk_root_id, chunk_parent_id, parent_block_number,
     chunk_size_measured, chunk_size_used, chunk_overlap_used, chunking_mode_used,
     chunk_norm_text, chunk_lemma_text, chunk_stem_text, embedding, embedding_model,
     embedding_dimension, embedding_provider, embedding_created_at, setting_id,
     ldts_local, ldts_gmt
FROM edu_ai.edu_ai_document_chunks;

DROP TABLE edu_ai.edu_ai_document_chunks;
ALTER TABLE edu_ai.edu_ai_document_chunks_new RENAME TO edu_ai_document_chunks;

-- PK + IDENTITY (canonical sequence name), then resume above the max copied id
ALTER TABLE edu_ai.edu_ai_document_chunks
    ADD CONSTRAINT edu_ai_document_chunks_pkey PRIMARY KEY (id);
ALTER TABLE edu_ai.edu_ai_document_chunks
    ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (SEQUENCE NAME edu_ai.edu_ai_document_chunks_id_seq);
SELECT setval('edu_ai.edu_ai_document_chunks_id_seq',
              GREATEST(coalesce((SELECT max(id) FROM edu_ai.edu_ai_document_chunks), 0), 1),
              (SELECT count(*) FROM edu_ai.edu_ai_document_chunks) > 0);

-- outbound FK
ALTER TABLE edu_ai.edu_ai_document_chunks
    ADD CONSTRAINT edu_ai_document_chunks_setting_id_fkey
    FOREIGN KEY (setting_id) REFERENCES edu_ai.search_setting (setting_id) ON UPDATE CASCADE;

-- indexes (HNSW vector + 3 GIN + setting)
CREATE INDEX idx_document_chunks_embedding_hnsw ON edu_ai.edu_ai_document_chunks
    USING hnsw (embedding edu_ai.vector_cosine_ops) WITH (m='16', ef_construction='64');
CREATE INDEX chunks_ts_norm_gin  ON edu_ai.edu_ai_document_chunks USING gin (ts_norm);
CREATE INDEX chunks_ts_lemma_gin ON edu_ai.edu_ai_document_chunks USING gin (ts_lemma);
CREATE INDEX chunks_ts_stem_gin  ON edu_ai.edu_ai_document_chunks USING gin (ts_stem);
CREATE INDEX chunks_setting_idx  ON edu_ai.edu_ai_document_chunks USING btree (setting_id);

COMMIT;
