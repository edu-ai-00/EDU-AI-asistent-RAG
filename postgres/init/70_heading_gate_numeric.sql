-- last change in code by Martin Safranek (2026-08-02).
-- 70_heading_gate_numeric.sql
-- A purely numeric concept may not open the heading gate.
--
-- "Jaké mám mít pomůcky do 7. třídy?" produces the concept `7`. Among chunk
-- headings a bare 7 is rare, so heading_max_df_share judged it discriminative and
-- the gate opened for a section headed
--
--     5. Vzdělávání učitelů v kariérovém poradenství > 7. …
--
-- at coverage 0.25 — and because a seeded document earns its own paragraph, an
-- answer about school supplies gained a paragraph about teacher training in career
-- counselling. Statistically the concept WAS rare; semantically a digit is an
-- ordinal fragment. It says which item, never what about.
--
-- The digit still counts towards coverage, where it does real work: it is what
-- distinguishes the 7th-grade supply list from the 8th.
--
-- heading_numeric_pass = true restores the previous behaviour.
--
-- Idempotent; transactional.

BEGIN;

UPDATE edu_ai.search_setting s
   SET config = jsonb_set(s.config, '{pipeline}', (
         SELECT jsonb_agg(CASE
                  WHEN step->>'step' = 'document_lexical'
                  THEN jsonb_set(step, '{params}',
                         coalesce(step->'params', '{}'::jsonb)
                         || jsonb_build_object('heading_numeric_pass', false), true)
                  ELSE step END ORDER BY ord)
         FROM jsonb_array_elements(s.config->'pipeline') WITH ORDINALITY e(step, ord)
       ), false)
 WHERE s.config->'pipeline' @> '[{"step": "document_lexical"}]'::jsonb
   AND NOT (s.config #> '{pipeline}'
            @> '[{"params": {"heading_numeric_pass": false}}]'::jsonb);

COMMIT;
