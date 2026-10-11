-- last change in code by Martin Safranek (2026-08-23).
--
-- THIN CHUNK GUARDS — a short passage that reached the pool on the embedding
-- alone may neither be judged nor found a paragraph.
--
-- Measured on "Co se hodnotí u přijímaček?" (question_answer_log 5255): two
-- chunks of an MŠMT form appendix, "HODNOCENÍ NA VYSVĚDČENÍCH Z PŘEDCHOZÍHO
-- VZDĚLÁVÁNÍ" (50 chars) and "Povinné předměty, povinně volitelné předměty"
-- (44), were graded 2 by the judge and promoted to positions 1 and 2, so the
-- answer was written from an empty form. Fusion had ranked them LAST of the top
-- six (0.117 / 0.193) — the ranking was right, which is why the guard sits in
-- the judge slice and in seeding, not in retrieval or fusion.
--
-- thin_max_chars      length limit, measured AFTER stripping the
--                     row_NN_column_NN: prefix and markdown (a table cell is
--                     mostly coordinates, and a naive test reads it as content)
-- thin_require_digit  optional tightener; off, because "Ředitelem školy je Jan
--                     Novák." carries a fact and no digit
-- thin_guard_judge    keep them out of the reranker's slice
-- thin_guard_seed     they may not represent a document (a floor in the code
--                     lets the best held-back chunk seed if NOTHING else did,
--                     so the guard can never turn an answer into no_answer)
--
-- config->pipeline is an ARRAY of steps, so the params are merged into the
-- element whose "step" is "answer" and the array is rebuilt. Per-org: every
-- active settings row is updated, the global one (legal_entity_code IS NULL)
-- included — a param on the global row alone would not reach a school.
BEGIN;

UPDATE edu_ai.settings s
   SET config = jsonb_set(s.config, '{pipeline}', (
         SELECT jsonb_agg(
                  CASE WHEN st->>'step' = 'answer'
                       THEN jsonb_set(st, '{params}',
                              coalesce(st->'params', '{}'::jsonb) || jsonb_build_object(
                                'thin_max_chars',     120,
                                'thin_require_digit', false,
                                'thin_guard_judge',   true,
                                'thin_guard_seed',    true), true)
                       ELSE st END
                  ORDER BY i)
           FROM jsonb_array_elements(s.config->'pipeline') WITH ORDINALITY t(st, i)), true)
 WHERE s.is_active;

COMMIT;
