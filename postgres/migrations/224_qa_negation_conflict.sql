-- 224: no curated answer across opposite polarity (negation_conflict on qa_lexical and qa_vector).
--
-- `negation_conflict` is a parameter of the qa_lexical and qa_vector steps read by
-- search/app/nodes/qa_lexical.py and qa_vector.py (off when absent). When the query and the
-- best entry hold the same word with opposite polarity ("Co když pozvánka nepřijde?" against
-- the curated "…přijde?") the confident answer is withheld; the entry may still be offered,
-- the asker sees its wording. A word is a negation only when the lemmatizer made it one
-- (eduai_normalize 3.4.0 is_negation, a set generated over its dictionary): "někdy" or
-- "nemoc", which read like "ne-kdy" and "ne-moc" once folded, no longer count.
--
-- Measured on 2026-11-10 (tools/lab/regression and tools/lab/variants, on top of 218-221 and 222,
-- ties settled; see LEDGER). The check was always on in this branch before; as a parameter it
-- is off until this migration:
--   JPZ set, 39 questions with the negation flipped: off 36 get the ORIGINAL answer confidently,
--   with 224 all 39 are only offered (the safety net it exists for).
--   regression set (3,477 cases): confident wrong 31 off and on; against the old always-on
--   check one case changes, for the better (a false conflict removed); false conflicts against
--   the right entry 73 -> 42 ("někdo", "nějak", "někde", "několik", "nemoc" no longer count).
--   gold set G of verified logged questions: unchanged.
-- NOT measured: qa_vector (needs a key; same function, unit-tested), other languages (no
-- negation prefix: never checked).
--
-- Settings are per organisation: every ACTIVE row with a qa_lexical or qa_vector step gets the
-- value. A step that already has negation_conflict is left alone.
-- Undo: remove negation_conflict from both steps' params (or set it to false).
--
-- Idempotent. No transaction control (runner, from 217).

UPDATE edu_ai.settings s
   SET config = jsonb_set(
         s.config, '{pipeline}',
         (SELECT jsonb_agg(
                   CASE WHEN st->>'step' IN ('qa_lexical', 'qa_vector')
                             AND jsonb_typeof(st->'params') = 'object'
                             AND NOT (st->'params' ? 'negation_conflict')
                        THEN jsonb_set(st, '{params,negation_conflict}', 'true'::jsonb, true)
                        ELSE st END
                   ORDER BY ord)
            FROM jsonb_array_elements(s.config->'pipeline') WITH ORDINALITY AS t(st, ord)))
 WHERE s.is_active
   AND jsonb_typeof(s.config->'pipeline') = 'array'
   AND EXISTS (SELECT 1 FROM jsonb_array_elements(s.config->'pipeline') st
                WHERE st->>'step' IN ('qa_lexical', 'qa_vector')
                  AND jsonb_typeof(st->'params') = 'object'
                  AND NOT (st->'params' ? 'negation_conflict'));

-- self-check: every active qa_lexical and qa_vector step carries negation_conflict
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM edu_ai.settings s, jsonb_array_elements(s.config->'pipeline') st
              WHERE s.is_active AND st->>'step' IN ('qa_lexical', 'qa_vector')
                AND jsonb_typeof(st->'params') = 'object'
                AND NOT (st->'params' ? 'negation_conflict')) THEN
    RAISE EXCEPTION '224: an active qa_lexical or qa_vector step has no negation_conflict';
  END IF;
END $$;
