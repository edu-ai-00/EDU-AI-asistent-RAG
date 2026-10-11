-- last change in code by Martin Safranek (2026-07-23).
-- 23_query_log_source_text.sql
-- Human-readable, two-dimensional decomposition of the query-log answer origin.
-- Keeps the raw granular columns (answer_source: qa_exact/qa_lexical/qa_vector/rag/
-- guard/none; strategies: [rag_lexical]…) for debugging, and adds derived text
-- columns for reporting:
--   answer_source_text : which knowledge base answered — QA | documents | guard | none
--   strategies_text    : which retrieval method       — exact | lexical | keywords | vector
-- Populated by search/app/log.py. match_score (already present) now also carries a
-- 0..1 relevance for the documents path (coverage/cosine/overlap-ratio).
-- Idempotent.

ALTER TABLE edu_ai.qa_query_log ADD COLUMN IF NOT EXISTS answer_source_text text;
ALTER TABLE edu_ai.qa_query_log ADD COLUMN IF NOT EXISTS strategies_text text;
