-- 132: drop the chunk_grade_doc_fallback trigger (2026-08-10, same day as 131).
-- Martin's review: the fallback belongs to the INSERT process in the shared
-- Markdown flow's insert component, where the whole grade chain already lives
-- (LLM enrichment -> deterministic patterns -> breadcrumb widening -> now the
-- document declaration as the last step) and where school_level is derived
-- from the resolved grades anyway. Ingest logic stays in the flow JSON as the
-- single source of truth; a DB trigger would be invisible duplicate magic.
-- The component edit ships in EDU_AI_LOAD_MARKDOWN.json (both insert
-- instances — text and table branch).

DROP TRIGGER IF EXISTS chunk_grade_doc_fallback ON edu_ai.document_chunks;
DROP FUNCTION IF EXISTS edu_ai.chunk_grade_doc_fallback();
