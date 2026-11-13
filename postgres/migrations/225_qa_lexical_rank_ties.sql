-- 225: ties in the qa_lexical ranking settled by the question text (rank_ties on).
--
-- `rank_ties` is a parameter of the qa_lexical step read by search/app/nodes/qa_lexical.py
-- (off when absent). Without it, candidates with an exactly equal rank come back in the order
-- the database stores them, which follows the entry ids and changes with every re-upload of
-- the packs: the same question can then get a different offer and, rarely, a different
-- confident answer. With it the cut at fetch_k keeps every variant tied with the last one
-- (FETCH FIRST n ROWS WITH TIES), an exact tie of tier, rank and coverage is settled by the
-- question text and then the id, and the typo fallback (pg_trgm, when nothing matches) is
-- ordered by the same rule. It changes which of two equally good entries wins, not how good
-- the answers are.
--
-- Measured with the regression set (tools/lab/regression, which has always run with it;
-- LEDGER 6 and 10 Nov): confident right and confident wrong answers are the same with and
-- without it; two identical runs agree completely, chips included (without it 4-6 of 3,558
-- questions moved between runs, and the small sets by up to 2.4 points). Cost: a few more
-- rows pulled at the fetch_k cut when there is a tie (latency not measured separately).
--
-- Settings are per organisation: every ACTIVE row with a qa_lexical step gets the value. A
-- step that already has rank_ties is left alone.
-- Undo: remove rank_ties from the qa_lexical params (or set it to false).
--
-- Idempotent. No transaction control (runner, from 217).

UPDATE edu_ai.settings s
   SET config = jsonb_set(
         s.config, '{pipeline}',
         (SELECT jsonb_agg(
                   CASE WHEN st->>'step' = 'qa_lexical'
                             AND jsonb_typeof(st->'params') = 'object'
                             AND NOT (st->'params' ? 'rank_ties')
                        THEN jsonb_set(st, '{params,rank_ties}', 'true'::jsonb, true)
                        ELSE st END
                   ORDER BY ord)
            FROM jsonb_array_elements(s.config->'pipeline') WITH ORDINALITY AS t(st, ord)))
 WHERE s.is_active
   AND jsonb_typeof(s.config->'pipeline') = 'array'
   AND EXISTS (SELECT 1 FROM jsonb_array_elements(s.config->'pipeline') st
                WHERE st->>'step' = 'qa_lexical'
                  AND jsonb_typeof(st->'params') = 'object'
                  AND NOT (st->'params' ? 'rank_ties'));

-- self-check: every active qa_lexical step carries rank_ties
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM edu_ai.settings s, jsonb_array_elements(s.config->'pipeline') st
              WHERE s.is_active AND st->>'step' = 'qa_lexical'
                AND jsonb_typeof(st->'params') = 'object'
                AND NOT (st->'params' ? 'rank_ties')) THEN
    RAISE EXCEPTION '225: an active qa_lexical step has no rank_ties';
  END IF;
END $$;
