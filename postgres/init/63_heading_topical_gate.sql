-- last change in code by Martin Safranek (2026-08-01).
-- 63_heading_topical_gate.sql
-- A concept found in a chunk's HEADING opens the coverage gate on its own.
--
-- What went wrong without it. "Jaké pomůcky potřebuje prvňáček?" produced four
-- concepts — jaky, pomucka, potrebovat, prvnak. The word "pomucka" exists in this
-- corpus in FIVE chunk headings and in NO chunk body, so the heading columns from
-- migration 61 were the entire signal, and they worked: the five chunks that are
-- about supplies were the five candidates. Then coverage threw all of them away at
-- 1/4 = 0.25 against a 0.50 floor, because the query also carried three concepts
-- that no document can ever contain. The same question phrased "Jaké mám mít
-- pomůcky?" passed at 1/2 = 0.50 — for no reason other than that `mit` happens to
-- be in the coverage stoplist. The gate was scoring the QUESTION's wording.
--
-- Coverage measures "what share of the query does this chunk cover". A heading hit
-- is not a share of anything; it is a statement that the chunk is ABOUT that topic.
-- So it gets its own door.
--
-- The door is not open to every word, or "Jak je to ve škole?" would match every
-- section whose heading says "škola". Discriminativeness is read from the data —
-- the share of chunks whose heading contains the concept:
--
--     pomucka   5/35 = 0.14   -> passes: names a topic
--     skola    13/35 = 0.37   -> blocked: says nothing about a section
--     trida    13/35 = 0.37   -> blocked
--
-- heading_max_df_share = 0.25 separates them. This is IDF-like but applied only
-- where it works: a concept PRESENT in some heading, so df > 0 by construction.
-- Plain IDF over the whole query would do the opposite — measured, `potrebovat`
-- and `prvnak` have df = 0 and therefore the HIGHEST IDF of the four, and weighted
-- coverage came out at 0.20, worse than the unweighted 0.25.
--
-- Chunks admitted this way are scored as covering that one concept fully, so they
-- do not out-rank a chunk that genuinely covers the whole query, and the fused
-- ranking still decides the order. heading_pass = false restores the old gate.
--
-- Idempotent; transactional.

BEGIN;

UPDATE edu_ai.search_setting s
   SET config = jsonb_set(s.config, '{pipeline}', (
         SELECT jsonb_agg(CASE
                  WHEN step->>'step' = 'document_lexical'
                  THEN jsonb_set(step, '{params}',
                         coalesce(step->'params', '{}'::jsonb)
                         || jsonb_build_object(
                              'heading_pass',         true,
                              'heading_max_df_share', 0.25),
                         true)
                  ELSE step END ORDER BY ord)
         FROM jsonb_array_elements(s.config->'pipeline') WITH ORDINALITY e(step, ord)
       ), false)
 WHERE s.config->'pipeline' @> '[{"step": "document_lexical"}]'::jsonb
   AND NOT (s.config #> '{pipeline}' @> '[{"params": {"heading_pass": true}}]'::jsonb);

COMMIT;
