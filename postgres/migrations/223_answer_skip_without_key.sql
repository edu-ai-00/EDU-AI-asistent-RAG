-- 223: without a chat key the answer step ends with the honest no-answer instead of a fault (skip_without_key).
--
-- `skip_without_key` is a parameter of the answer step read by search/app/nodes/answer.py and
-- the streaming path in search/app/server.py (off when absent). It acts ONLY when
-- OPENAI_API_KEY is absent or still the installer's placeholder (CHANGE_ME…, the rule of
-- scripts/envfile.is_placeholder): every generation fails then, and the asker used to read
-- "llm_unavailable" ("Dočasný technický problém" in the chat). With it the step ends as if no
-- passage had been found: the offer parked by qa_lexical, else no_answer with the fallback
-- questions. With a real key nothing changes, so the value is safe on every installation.
--
-- Measured on 2026-11-08 by replaying a production log with its documents, without a key
-- (normalization 3.3.0, 218-221): 1,073 of 4,442 questions (24 %) ended in llm_unavailable,
-- every one a qa_lexical decision C with document passages gathered and a failed generation.
-- With this migration a sample of those questions ends in no_answer with the same fallback
-- questions, and the other questions do not change (tools/lab/regression/LEDGER.md, 8 Nov).
-- NOT measured: nothing with a key (the parameter does not act there; unit-tested).
--
-- Settings are per organisation: every ACTIVE row with an answer step gets the value. A step
-- that already has skip_without_key is left alone.
-- Undo: remove skip_without_key from the answer params (or set it to false).
--
-- Idempotent. No transaction control (runner, from 217).

UPDATE edu_ai.settings s
   SET config = jsonb_set(
         s.config, '{pipeline}',
         (SELECT jsonb_agg(
                   CASE WHEN st->>'step' = 'answer'
                             AND jsonb_typeof(st->'params') = 'object'
                             AND NOT (st->'params' ? 'skip_without_key')
                        THEN jsonb_set(st, '{params,skip_without_key}', 'true'::jsonb, true)
                        ELSE st END
                   ORDER BY ord)
            FROM jsonb_array_elements(s.config->'pipeline') WITH ORDINALITY AS t(st, ord)))
 WHERE s.is_active
   AND jsonb_typeof(s.config->'pipeline') = 'array'
   AND EXISTS (SELECT 1 FROM jsonb_array_elements(s.config->'pipeline') st
                WHERE st->>'step' = 'answer'
                  AND jsonb_typeof(st->'params') = 'object'
                  AND NOT (st->'params' ? 'skip_without_key'));

-- self-check: every active answer step carries skip_without_key
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM edu_ai.settings s, jsonb_array_elements(s.config->'pipeline') st
              WHERE s.is_active AND st->>'step' = 'answer'
                AND jsonb_typeof(st->'params') = 'object'
                AND NOT (st->'params' ? 'skip_without_key')) THEN
    RAISE EXCEPTION '223: an active answer step has no skip_without_key';
  END IF;
END $$;
