-- 221: qa_lexical pulls 200 candidate variants before the entry aggregation, not 50 (fetch_k: 200).
--
-- `fetch_k` is a parameter of the qa_lexical step that the code already reads
-- (`params.get("fetch_k", config.QA_LEXICAL_FETCH_K)`, default 50). It is the number of
-- VARIANT rows (the canonical question and every alternative) taken by ts_rank_cd before
-- they are aggregated to entries. Coverage is computed against the union of the lemmas of
-- the variants that were pulled, so with 4-5 variants per entry a low value cuts variants
-- of the right entry off, the union is incomplete and the coverage is under-estimated.
-- Nothing in the code changes; only the value in the settings.
--
-- Measured on 2026-11-03 with the regression set (tools/lab/regression), on top of 218-220,
-- QA only, scoring confident right +1, right one in the offer +0.5, confident wrong -3,
-- by overriding the parameter for the run:
--   fetch_k   score    confident wrong   better / worse   sees the right answer (A / B / D / L)
--   50        1818.5   31                                  83.7 / 74.0 / 45.4 / 40.0 %
--   100       1841.0   31                48 / 3            84.8 / 75.7 / 48.9 / 42.4 %
--   200       1853.5   31                74 / 4            85.6 / 76.4 / 52.3 / 42.4 %
--   400       1855.0   31                80 / 7            85.7 / 76.5 / 51.1 / 43.5 %
-- With this migration itself (the value read from the settings in the database) the same set gives
-- score 1853.0 (+34.5, interval +26 ... +43.5), 72 cases better, 3 worse, no new confident wrong
-- answer, no silent loss of the lead over the second entry, and the asker sees the right answer
-- A 85.4 / B 76.1 / D 51.1 / L 45.9 % (the two runs differ only in which 3 questions are offered
-- in 25 cases without a confident answer; the cause is not found). The gain flattens beyond 200. The value was chosen on the train
-- halves of the sets (A:train +10.0, B:train +15.5); the held-out halves agree on the direction.
-- NOT measured: the latency of pulling 200 rows instead of 50, other schools, other languages.
--
-- Settings are per organisation: every ACTIVE row with a qa_lexical step gets the value. A
-- value that is already 200 or higher is left alone.
-- Undo: set fetch_k of the qa_lexical params back to 50 (or remove it for the code default).
--
-- Idempotent. No transaction control (runner, from 217).

UPDATE edu_ai.settings s
   SET config = jsonb_set(
         s.config, '{pipeline}',
         (SELECT jsonb_agg(
                   CASE WHEN st->>'step' = 'qa_lexical'
                             AND jsonb_typeof(st->'params') = 'object'
                             AND CASE WHEN jsonb_typeof(st->'params'->'fetch_k') = 'number'
                                      THEN (st->'params'->>'fetch_k')::numeric < 200
                                      ELSE true END
                        THEN jsonb_set(st, '{params,fetch_k}', '200'::jsonb, true)
                        ELSE st END
                   ORDER BY ord)
            FROM jsonb_array_elements(s.config->'pipeline') WITH ORDINALITY AS t(st, ord)))
 WHERE s.is_active
   AND jsonb_typeof(s.config->'pipeline') = 'array'
   AND EXISTS (SELECT 1 FROM jsonb_array_elements(s.config->'pipeline') st
                WHERE st->>'step' = 'qa_lexical'
                  AND jsonb_typeof(st->'params') = 'object'
                  AND CASE WHEN jsonb_typeof(st->'params'->'fetch_k') = 'number'
                           THEN (st->'params'->>'fetch_k')::numeric < 200
                           ELSE true END);

-- self-check: every active qa_lexical step pulls at least 200 candidate variants
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM edu_ai.settings s, jsonb_array_elements(s.config->'pipeline') st
              WHERE s.is_active AND st->>'step' = 'qa_lexical'
                AND jsonb_typeof(st->'params') = 'object'
                AND CASE WHEN jsonb_typeof(st->'params'->'fetch_k') = 'number'
                         THEN (st->'params'->>'fetch_k')::numeric < 200
                         ELSE true END) THEN
    RAISE EXCEPTION '221: an active qa_lexical step still pulls fewer than 200 candidate variants';
  END IF;
END $$;
