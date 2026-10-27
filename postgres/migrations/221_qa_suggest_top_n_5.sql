-- 221: the offer "Nechtěl ses zeptat…?" holds up to 5 curated questions, not 3.
--
-- For 37 % of the independent test queries the right answer is among the
-- nearest curated questions but not sure enough to be given outright, and it
-- sits on the 4th or 5th place often enough to matter. Measured on 2026-10-27
-- with the regression set (tools/lab/regression) on lemma only + 218 + 219 + 220,
-- changing only suggest_top_n in qa_lexical:
--   * the asker sees the right answer (confident, in the offer or parked as the
--     nearest questions) for 84 % → 87 % of set A (1 470 paraphrases) and
--     74 % → 77 % of the independent set B (1 240 queries by other authors)
--   * confident right answers and confident wrong answers do not change at all
--     (A 598 / 10, B 261 / 9): the offer only grows, no decision moves.
-- The web chat renders every suggestion it receives (no limit of its own), so
-- it shows up to 5 buttons.
--
-- Only the qa_lexical step changes. qa_exact (related questions beside an exact
-- answer) falls back to the qa_lexical value, so it gets 5 too — that is the
-- same row of the configuration, see docs/SETTINGS_REFERENCE.md §3.2.
-- Settings are per organisation: every ACTIVE row with a qa_lexical step.
-- Undo: set suggest_top_n of qa_lexical back to 3.
--
-- Idempotent. No transaction control (runner, from 217).

UPDATE edu_ai.settings s
   SET config = jsonb_set(
         s.config, '{pipeline}',
         (SELECT jsonb_agg(
                   CASE WHEN st->>'step' = 'qa_lexical'
                        THEN jsonb_set(st, '{params,suggest_top_n}', '5'::jsonb, true)
                        ELSE st END
                   ORDER BY ord)
            FROM jsonb_array_elements(s.config->'pipeline') WITH ORDINALITY AS t(st, ord)))
 WHERE s.is_active
   AND jsonb_typeof(s.config->'pipeline') = 'array'
   AND EXISTS (SELECT 1 FROM jsonb_array_elements(s.config->'pipeline') st
                WHERE st->>'step' = 'qa_lexical'
                  AND jsonb_typeof(st->'params') = 'object'
                  AND st->'params'->'suggest_top_n' IS DISTINCT FROM '5'::jsonb);

-- self-check: every active qa_lexical step offers 5
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM edu_ai.settings s, jsonb_array_elements(s.config->'pipeline') st
              WHERE s.is_active AND st->>'step' = 'qa_lexical'
                AND st->'params'->'suggest_top_n' IS DISTINCT FROM '5'::jsonb) THEN
    RAISE EXCEPTION '221: an active qa_lexical step does not have suggest_top_n 5';
  END IF;
END $$;
