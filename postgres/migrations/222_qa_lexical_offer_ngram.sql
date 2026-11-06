-- 222: the last chip of the qa_lexical offer may come from character n-grams (offer_ngram).
--
-- `offer_ngram` is a parameter of the qa_lexical step read by search/app/nodes/qa_ngram.py
-- (off when absent). When there is no confident answer, the "did you mean" list or the
-- dead-end fallback keeps its first chips and the LAST place goes to the curated question
-- whose wording is closest in character 3-5-grams (TF-IDF over the packs in scope, each
-- wording of an entry scored separately), if the similarity reaches min_sim 0.30. Words of
-- 1-2 letters, numbers, stop-words, question words and words held by more than 10 % of the
-- entries do not count. Confident answers, the first chips and the chips beside an answer
-- never change. It catches derived and colloquial forms the lemma misses ("třídnické" →
-- "třídní", "matika" → "matematika"); it cannot help where the words differ completely.
--
-- Measured on 2026-11-06 with the regression set (tools/lab/regression), on top of 218-221,
-- QA only, PAIRED (both variants asked in one transaction; separate runs of the same
-- configuration differ by up to ±2.4 points on the small sets, cause not found, see LEDGER):
--   sees the right answer (confident, offer or fallback), better / worse cases:
--   A:train 86.2 -> 89.6 % (+47/-10)   A:holdout 82.6 -> 83.7 % (+9/-5)
--   B:train 77.1 -> 79.7 % (+30/-6)    B:holdout 73.0 -> 75.7 % (+14/-6)
--   D:train 53.8 -> 59.0 % (+4/-0)     L:train 44.8 -> 55.2 % (+6/-0)   L:holdout 48.1 -> 51.9 % (+2/-1)
--   gold set of verified logged questions G:train 56.1 -> 65.2 % (+7/-1)
--   out-of-content questions that get an extra chip: 4.7 % (train) / 4.8 % (holdout)
--   decisions changed 0, confident answers changed 0, score +22.5
-- min_sim 0.25 adds one case on D:train and doubles the extra chips for out-of-content
-- questions (10 %); 0.35 loses a third of the gain. A log replay of 2,446 real queries:
-- decisions changed 0, 389 of the 947 queries without a confident answer get a new third chip.
-- Latency 0.5 ms per query; the index (0.13 s per school) is rebuilt every 5 minutes.
-- NOT measured: other schools, other languages (the stop-words come from the settings of
-- each language, the n-grams themselves are language-agnostic).
--
-- Settings are per organisation: every ACTIVE row with a qa_lexical step gets the value. A
-- step that already has offer_ngram is left alone.
-- Undo: remove offer_ngram from the qa_lexical params (or set it to false).
--
-- Idempotent. No transaction control (runner, from 217).

UPDATE edu_ai.settings s
   SET config = jsonb_set(
         s.config, '{pipeline}',
         (SELECT jsonb_agg(
                   CASE WHEN st->>'step' = 'qa_lexical'
                             AND jsonb_typeof(st->'params') = 'object'
                             AND NOT (st->'params' ? 'offer_ngram')
                        THEN jsonb_set(st, '{params,offer_ngram}',
                                       '{"min_sim": 0.30, "max_df_share": 0.10}'::jsonb, true)
                        ELSE st END
                   ORDER BY ord)
            FROM jsonb_array_elements(s.config->'pipeline') WITH ORDINALITY AS t(st, ord)))
 WHERE s.is_active
   AND jsonb_typeof(s.config->'pipeline') = 'array'
   AND EXISTS (SELECT 1 FROM jsonb_array_elements(s.config->'pipeline') st
                WHERE st->>'step' = 'qa_lexical'
                  AND jsonb_typeof(st->'params') = 'object'
                  AND NOT (st->'params' ? 'offer_ngram'));

-- self-check: every active qa_lexical step carries offer_ngram
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM edu_ai.settings s, jsonb_array_elements(s.config->'pipeline') st
              WHERE s.is_active AND st->>'step' = 'qa_lexical'
                AND jsonb_typeof(st->'params') = 'object'
                AND NOT (st->'params' ? 'offer_ngram')) THEN
    RAISE EXCEPTION '222: an active qa_lexical step has no offer_ngram';
  END IF;
END $$;
