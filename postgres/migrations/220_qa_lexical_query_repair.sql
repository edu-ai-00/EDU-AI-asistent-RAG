-- 220: turn on the typo repair of the query in qa_lexical (query_repair: ["typo"]).
--
-- A word the lemmatizer leaves as it is AND the dictionary does not know
-- ("abesnce") becomes the ONE word of the school's curated questions within edit
-- distance 1 (2 from 8 letters); a tie or nothing near leaves the query alone
-- (search/app/nodes/query_repair.py, tests/test_query_repair.py). It replaces
-- what the stem used to do by accident (a shared stem matched a typo) without
-- its false matches: migration 219 takes the stem out of the QA match.
--
-- Measured on 2026-10-26 with the regression set (tools/lab/regression) on top
-- of lemma only (219) + 218, QA only, scoring confident right +1, right one in
-- the offer +0.5, confident wrong −3:
--   * independent written set B (1 240 queries by other authors): 433 → 462,
--     confident right 225 → 260, confident wrong 9 → 9
--   * set A (1 470 paraphrases, typos among them): 816.5 → 833.5, wrong 9 → 10
--   * log replay (4 442 logged questions, earlier measurement): +7 right, 6
--     wrong. The wrong ones are not the repair: "muzu mit ve skole mobinlí
--     telefon" lands on the JPZ entry through ranking and the data, which an
--     alternative wording fixes.
--
-- Settings are per organisation: every ACTIVE row with a qa_lexical step gets
-- the parameter. The code ignores the parameter until the search image holds
-- query_repair.py, so the order of image and migration does not matter.
-- Undo: remove "query_repair" from the qa_lexical params.
--
-- Idempotent. No transaction control (runner, from 217).

UPDATE edu_ai.settings s
   SET config = jsonb_set(
         s.config, '{pipeline}',
         (SELECT jsonb_agg(
                   CASE WHEN st->>'step' = 'qa_lexical'
                        THEN jsonb_set(st, '{params,query_repair}', '["typo"]'::jsonb, true)
                        ELSE st END
                   ORDER BY ord)
            FROM jsonb_array_elements(s.config->'pipeline') WITH ORDINALITY AS t(st, ord)))
 WHERE s.is_active
   AND jsonb_typeof(s.config->'pipeline') = 'array'
   AND EXISTS (SELECT 1 FROM jsonb_array_elements(s.config->'pipeline') st
                WHERE st->>'step' = 'qa_lexical'
                  AND jsonb_typeof(st->'params') = 'object'
                  AND st->'params'->'query_repair' IS DISTINCT FROM '["typo"]'::jsonb);

-- self-check: every active qa_lexical step has exactly the typo repair
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM edu_ai.settings s, jsonb_array_elements(s.config->'pipeline') st
              WHERE s.is_active AND st->>'step' = 'qa_lexical'
                AND st->'params'->'query_repair' IS DISTINCT FROM '["typo"]'::jsonb) THEN
    RAISE EXCEPTION '220: an active qa_lexical step is without query_repair ["typo"]';
  END IF;
END $$;
