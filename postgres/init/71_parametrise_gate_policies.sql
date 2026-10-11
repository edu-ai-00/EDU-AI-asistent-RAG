-- last change in code by Martin Safranek (2026-08-02).
-- 71_parametrise_gate_policies.sql
-- The four decisions that shaped this round's results were still constants in the
-- code. Each one is a POLICY — a choice that could defensibly be different — not a
-- fact about the data, so each becomes config. Values equal the current behaviour;
-- activating this changes nothing.
--
-- document_lexical.max_concepts (8)
--     How many query concepts reach the SQL (one @@ each) and the coverage gate.
--     On a long question everything beyond the cap is invisible to both, so this
--     decides which parts of a question are allowed to matter.
--
-- document_lexical.rank_tiebreak_weight (0.001) + rank_tiebreak_cap (0.999)
--     score = coverage + min(ts_rank, cap) * weight. At 0.001 coverage always beats
--     word density — and that is precisely what put the entrance-exam sections
--     (ts_rank 3.6) ahead of the school's supply lists (1.8) when both had equal
--     coverage and an equal heading hit. Raising the weight lets density speak; the
--     cap stops one unbounded rank from crossing a whole coverage step.
--
-- rag_keywords/rag_vector.heading_quality (1.0)
--     What a heading match is worth when the executor asks "is the pool good
--     enough to stop gathering?". A heading-admitted chunk carries coverage 0.25,
--     which the executor used to read as weak, so it kept falling through to the
--     alternative retrievers with evidence it had itself called sufficient. At 1.0
--     the cascade stops; lower it to keep the fallbacks running.
--
-- answer.seed_scan_depth (15 = content_top_n x sources_top_n)
--     How deep into the ranked pool a document may still be found and earn its own
--     paragraph. This exact number decided whether the school's supply lists —
--     candidates 6 to 10 — reached the answer at all.
--
-- Idempotent; transactional.

BEGIN;

UPDATE edu_ai.search_setting s
   SET config = jsonb_set(s.config, '{pipeline}', (
         SELECT jsonb_agg(CASE
                  WHEN step->>'step' = 'document_lexical'
                  THEN jsonb_set(step, '{params}',
                         coalesce(step->'params', '{}'::jsonb)
                         || jsonb_build_object('max_concepts', 8,
                                               'rank_tiebreak_weight', 0.001,
                                               'rank_tiebreak_cap', 0.999), true)
                  WHEN step->>'step' IN ('rag_keywords', 'rag_vector')
                  THEN jsonb_set(step, '{params}',
                         coalesce(step->'params', '{}'::jsonb)
                         || jsonb_build_object('heading_quality', 1.0), true)
                  WHEN step->>'step' = 'answer'
                  THEN jsonb_set(step, '{params}',
                         coalesce(step->'params', '{}'::jsonb)
                         || jsonb_build_object('seed_scan_depth', 15), true)
                  ELSE step END ORDER BY ord)
         FROM jsonb_array_elements(s.config->'pipeline') WITH ORDINALITY e(step, ord)
       ), false)
 WHERE s.config ? 'pipeline'
   AND NOT (s.config #> '{pipeline}' @> '[{"params": {"max_concepts": 8}}]'::jsonb);

COMMIT;
