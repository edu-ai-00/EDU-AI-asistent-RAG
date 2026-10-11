-- last change in code by Martin Safranek (2026-08-01).
-- 58_answer_context_params.sql
-- content_top_n / sources_top_n / token_budget move into the `answer` step params.
--
-- They were os.getenv() constants that nothing ever set, so in practice they were
-- hardcoded — while the model and the output cap sitting right next to them were
-- versioned config. That line was in the wrong place: these three decide WHAT the
-- answer is built from. content_top_n is why a chunk ranked 6th never reaches the
-- prompt no matter how good the retrieval was, and diagnosing that required
-- reading the source and rebuilding the image to try another value.
--
--   content_top_n  chunks shown to the model (the pool is usually far larger)
--   sources_top_n  documents the answer may cite, and therefore draw chunks from
--   token_budget   context size; char budget = token_budget * 4
--
-- Values are the previous code defaults, so activating this changes nothing.
-- Resolution stays three-level: step params -> env -> code default.
--
-- Applied to every setting version, so a rollback keeps the same behaviour.
-- Idempotent; transactional.

BEGIN;

UPDATE edu_ai.search_setting s
   SET config = jsonb_set(s.config, '{pipeline}', (
         SELECT jsonb_agg(
                  CASE WHEN step->>'step' = 'answer'
                       THEN jsonb_set(step, '{params}',
                              coalesce(step->'params', '{}'::jsonb)
                              || jsonb_build_object('content_top_n', 5,
                                                    'sources_top_n', 3,
                                                    'token_budget', 8000), true)
                       ELSE step
                  END ORDER BY ord)
         FROM jsonb_array_elements(s.config->'pipeline') WITH ORDINALITY AS e(step, ord)), false)
 WHERE s.config->'pipeline' @> '[{"step": "answer"}]'::jsonb
   AND NOT (s.config->'pipeline' @> '[{"params": {"content_top_n": 5}}]'::jsonb);

COMMIT;
