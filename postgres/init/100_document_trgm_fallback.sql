-- last change in code by Martin Safranek (2026-08-03).
-- 100_document_trgm_fallback.sql
-- Trigram indexes for the document typo fallback.
--
-- QA has had a typo net from the start: when the tsquery matches nothing,
-- qa_lexical falls back to pg_trgm similarity over qa_variants.norm_text
-- (indexed since init 07). Documents had NO equivalent — a typo'd or
-- diacritics-less query left document_lexical empty and everything hung on the
-- embedding step. This adds the same net for chunks, using word_similarity
-- (the <% operator — short query vs LONG text, unlike plain similarity which
-- normalises by both lengths and never fires on a 900-char chunk).
--
-- Two indexes, not one: headings are NOT part of chunk_text since migration 61
-- (they live in their own weighted columns), and the supply lists are exactly
-- the case where the query's word sits in the heading alone — a body-only net
-- would miss the very chunks this exists to catch.
--
-- The operator class is schema-qualified: pg_trgm lives IN the edu_ai schema
-- (01_schema.sql) and this script runs under the default search_path.
--
-- Idempotent.

CREATE INDEX IF NOT EXISTS chunks_norm_trgm
    ON edu_ai.document_chunks USING gin (chunk_norm_text edu_ai.gin_trgm_ops);

CREATE INDEX IF NOT EXISTS chunks_heading_norm_trgm
    ON edu_ai.document_chunks USING gin (section_heading_norm edu_ai.gin_trgm_ops);
