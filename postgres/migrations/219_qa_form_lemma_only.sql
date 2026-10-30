-- 219: the curated-QA steps match on the lemma only (form ["norm","lemma"]), no stem.
--
-- Settings are per organisation: every ACTIVE settings row gets the change, so
-- no school keeps a different matching form by accident.
--
-- Why. Stemming matches by a shared stem and pays for it with false matches
-- ("pořadí" → a question about "dohled", "přílohy k přihlášce" → "další kolo").
-- Measured on 2026-10-26, QA only (exact + lexical match):
--   * the log replay (4 442 logged questions): lemma only vs lemma+stem —
--     34 fewer wrong answers, 11 fewer right ones (they stay in the offer);
--     stem only: 33 fewer right, 51 more wrong
--   * regression set (tools/lab/regression), scored confident right +1, right
--     one in the offer +0.5, confident wrong −3: independent set B (1 240
--     queries by other authors) 400.5 → 409, confident wrong answers 13 → 9;
--     set A (1 470 paraphrases) 742 → 732, wrong 11 → 9 (the 218 stop-words
--     below more than make that up: A 816.5)
-- Only the QA steps change. The document steps (document_lexical, rag_keywords)
-- keep lemma+stem: not measured, and without the embeddings a stem is all the
-- recall they have.
--
-- Not covered: the stored stem columns stay (index unchanged), they are just
-- not matched by the QA steps. Undo: set the form of both steps back to
-- ["norm","lemma","stem"].
--
-- Idempotent: only a form that still holds the stem is changed.
-- No transaction control (runner, from 217).

UPDATE edu_ai.settings s
   SET config = jsonb_set(
         s.config, '{pipeline}',
         (SELECT jsonb_agg(
                   CASE WHEN st->>'step' IN ('qa_exact', 'qa_lexical')
                             AND st->'params'->'form' @> '"stem"'::jsonb
                        THEN jsonb_set(st, '{params,form}', '["norm","lemma"]'::jsonb)
                        ELSE st END
                   ORDER BY ord)
            FROM jsonb_array_elements(s.config->'pipeline') WITH ORDINALITY AS t(st, ord)))
 WHERE s.is_active
   AND jsonb_typeof(s.config->'pipeline') = 'array'
   AND EXISTS (SELECT 1 FROM jsonb_array_elements(s.config->'pipeline') st
                WHERE st->>'step' IN ('qa_exact', 'qa_lexical')
                  AND st->'params'->'form' @> '"stem"'::jsonb);

-- self-check: no active setting matches the QA on a stem, and the document steps are untouched
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM edu_ai.settings s, jsonb_array_elements(s.config->'pipeline') st
              WHERE s.is_active AND st->>'step' IN ('qa_exact', 'qa_lexical')
                AND st->'params'->'form' @> '"stem"'::jsonb) THEN
    RAISE EXCEPTION '219: an active setting still matches the QA on the stem';
  END IF;
  IF EXISTS (SELECT 1 FROM edu_ai.settings s, jsonb_array_elements(s.config->'pipeline') st
              WHERE s.is_active AND st->>'step' IN ('qa_exact', 'qa_lexical')
                AND st->'params'->'form' IS NOT NULL
                AND NOT (st->'params'->'form' @> '"lemma"'::jsonb)) THEN
    RAISE EXCEPTION '219: a QA step lost the lemma form';
  END IF;
END $$;
