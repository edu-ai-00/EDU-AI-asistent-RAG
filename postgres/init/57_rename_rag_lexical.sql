-- last change in code by Martin Safranek (2026-08-01).
-- 57_rename_rag_lexical.sql
-- Pipeline step rag_lexical -> document_lexical.
--
-- The name now says which INDEX the step searches (document chunks) rather than
-- which architecture it belongs to — "rag_" described everything and therefore
-- nothing, while its sibling qa_lexical already named its index.
--
-- Renamed in EVERY setting, not just the active one: an inactive version is a
-- rollback target, and a rollback to a config naming a step the code no longer
-- registers would silently skip document retrieval altogether (the executor logs
-- "unknown step" and moves on). The same reason the code stopped deriving
-- behaviour from the "rag_" prefix — see GATHERERS in executor.py.
--
-- qa_query_log is append-only and NOT rewritten: rows logged before this carry
-- the old name in `strategies` / `flow_trace`, which is what actually ran then.
--
-- Idempotent (a second run finds no rag_lexical left); transactional.

BEGIN;

UPDATE edu_ai.search_setting s
   SET config = jsonb_set(s.config, '{pipeline}', (
         SELECT jsonb_agg(
                  CASE WHEN step->>'step' = 'rag_lexical'
                       THEN jsonb_set(step, '{step}', '"document_lexical"'::jsonb)
                       ELSE step
                  END ORDER BY ord)
         FROM jsonb_array_elements(s.config->'pipeline') WITH ORDINALITY AS e(step, ord)), false)
 WHERE s.config->'pipeline' @> '[{"step": "rag_lexical"}]'::jsonb;

COMMIT;
