-- last change in code by Martin Safranek (2026-08-03).
-- 101_result_document_cap.sql
-- GLOBAL cap on how many source documents a search RESPONSE carries.
--
--   config -> search -> max_result_documents   (global settings row only,
--                                               like search_requires_user)
--
-- Applied to the FINISHED result, not to retrieval: the answer is still
-- composed from `sources_top_n` documents (an answer-step param, per
-- organisation), and only the response is trimmed — the sources list keeps its
-- first N entries and a paragraph whose document fell over the cap keeps its
-- TEXT but loses the link (a wrong or dangling link is worse than none).
-- That split is deliberate: how an answer is BUILT is retrieval tuning per
-- organisation; how much a client SHOWS is one platform-wide presentation
-- decision, so it lives globally and the code reads the global row only.
--
-- Seeded at 3 = today's effective behaviour (sources_top_n is 3 everywhere),
-- so nothing changes until somebody lowers it. NULL / absent = no cap.
--
-- Idempotent; transactional.

BEGIN;

UPDATE edu_ai.settings s
   SET config = jsonb_set(
         s.config, '{search}',
         coalesce(s.config->'search', '{}'::jsonb)
         || jsonb_build_object('max_result_documents', 3))
 WHERE s.legal_entity_code IS NULL
   AND NOT coalesce(s.config->'search', '{}'::jsonb) ? 'max_result_documents';

COMMIT;
