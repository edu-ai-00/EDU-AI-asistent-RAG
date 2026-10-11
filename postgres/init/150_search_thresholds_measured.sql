-- last change in code by Martin Safranek (2026-08-18).
-- 150: qa_vector's cosine bar comes down to a MEASURED value, and three knobs
--      that decided behaviour from code defaults become tunable data.
--
-- 1) qa_vector.cosine_min 0.75 -> 0.65
--
-- Measured on the stored vectors themselves (no LLM, no new embeddings — the
-- pairs already exist in qa_embedding), over one pack's variants:
--
--     pairs of variants of  |  min   p05   median  p95    max
--     THE SAME question     | 0.186  0.287  0.495  0.833  0.896
--     DIFFERENT questions   | 0.106  0.184  0.298  0.484  0.616
--
-- 0.75 sits ABOVE the median AND the p95 of the pairs that SHOULD match: it
-- admitted roughly the top tenth of them. That is why log 2730 ("uvolnění ze
-- školy" against the curated "Jak požádat o uvolnění z výuky na několik dní?")
-- scored 0.6243 and was rejected, answering from documents in 5.9 s instead of
-- from the curated entry in milliseconds. 0.62 is not an outlier for Czech with
-- text-embedding-3-small — it is an ordinary value.
--
-- 0.65 is chosen from the data, not from taste: it is ABOVE the maximum of the
-- pairs that should NOT match (0.616), so nothing the measurement calls unrelated
-- gets through, while roughly the top third of the true pairs now does.
--
-- It also narrows an asymmetry the audit exposed: documents were admitted from
-- cosine 0.45 while a CURATED answer needed 0.75 — 1.7x stricter for the safer
-- source, which is backwards.
--
-- CAVEAT, on purpose in the migration and not only in chat: the distribution was
-- measured on synthetic test packs (10 questions, ~4 variants each; 80 true and
-- 910 false pairs). The separation is clear but the numbers will shift on a real
-- curated pack, where a curator's paraphrases sit closer together. Re-run the
-- measurement after the first real pack lands — it costs nothing, the vectors are
-- already stored — and confirm or move the bar.
--
-- 2) Three knobs that were data-shaped but lived as code defaults
--
-- The code already reads all three through params.get(), so nothing in the search
-- service changes: the DB simply never set them, which meant changing them
-- required editing Python and rebuilding the image.
--   answer.grade_scope             "focus_if_match" — what a KNOWN ročník does to
--                                  the pool (focus vs pure preference)
--   answer.grade_chunk_min_ratio   0.8 — the guard that stops a weak exact-ročník
--                                  chunk from leading on its tag alone
--   *.topical_admission            true — the gate that refuses a chunk matching
--                                  only a generic concept
-- Values are the current code defaults, so this migration changes NO behaviour
-- here; it moves the dial from source to data.
--
-- Applied to EVERY active settings row. Search config is per legal entity (four
-- active rows today: global + three schools) and a knob added to one row only
-- would silently keep the old default everywhere else — measured the hard way on
-- child_may_publish_organization.

SET search_path TO edu_ai, public;

BEGIN;

UPDATE settings s
   SET config = jsonb_set(
         s.config::jsonb, '{pipeline}',
         (SELECT jsonb_agg(
                   CASE
                     WHEN x->>'step' = 'qa_vector'
                       THEN jsonb_set(x, '{params,cosine_min}', '0.65'::jsonb, true)
                     WHEN x->>'step' = 'answer'
                       THEN jsonb_set(
                              jsonb_set(x, '{params,grade_scope}',
                                        '"focus_if_match"'::jsonb, true),
                              '{params,grade_chunk_min_ratio}', '0.8'::jsonb, true)
                     WHEN x->>'step' IN ('document_lexical', 'rag_keywords')
                       THEN jsonb_set(x, '{params,topical_admission}',
                                      'true'::jsonb, true)
                     ELSE x
                   END ORDER BY ord)
            FROM jsonb_array_elements(s.config::jsonb->'pipeline')
                 WITH ORDINALITY AS t(x, ord))
       )::json
 WHERE s.is_active;

-- fail loudly rather than leave some rows behind
DO $$
DECLARE bad int;
BEGIN
    SELECT count(*) INTO bad
      FROM settings s, jsonb_array_elements(s.config::jsonb->'pipeline') AS x
     WHERE s.is_active AND x->>'step' = 'qa_vector'
       AND (x->'params'->>'cosine_min') IS DISTINCT FROM '0.65';
    IF bad > 0 THEN
        RAISE EXCEPTION '% active row(s) still not at cosine_min 0.65', bad;
    END IF;
    SELECT count(*) INTO bad
      FROM settings s, jsonb_array_elements(s.config::jsonb->'pipeline') AS x
     WHERE s.is_active AND x->>'step' = 'answer'
       AND ((x->'params'->>'grade_scope') IS NULL
         OR (x->'params'->>'grade_chunk_min_ratio') IS NULL);
    IF bad > 0 THEN
        RAISE EXCEPTION '% active row(s) missing the answer grade knobs', bad;
    END IF;
END $$;

COMMIT;
