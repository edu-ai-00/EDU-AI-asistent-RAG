-- last change in code by Martin Safranek (2026-08-20).
-- 153: the two bare numbers that decided source attribution get names and a home.
--
-- answer.py had `ov[i] >= 0.15` and `ov[best] >= 0.25` sitting inside one
-- condition — the only place in the whole system where those numbers existed.
-- They decide which SOURCE DOCUMENT each paragraph of the answer is credited to,
-- which is what the user sees under the answer as a clickable link, and they were:
--   * unnamed, so nothing in the code said what they meant,
--   * absent from settings, so an audit of "what can be tuned" did not list them,
--   * and only changeable by editing Python and rebuilding the image.
--
-- WHAT THEY DO
--   attrib_positional_min (0.15) — a paragraph in slot i is credited to document i
--       when its own words overlap that document by at least this much. The bar is
--       LOW on purpose: the ordering is already evidence, and the overlap only has
--       to avoid contradicting it.
--   attrib_best_min (0.25) — any OTHER document must earn the paragraph on content
--       alone, so its bar is higher.
--
-- INVARIANT: attrib_best_min > attrib_positional_min. Equal values make the
-- positional branch redundant; reversed, a weakly-overlapping foreign document
-- outranks the paragraph order and the answer starts citing the wrong source.
-- Nothing enforces this in code — it is stated here so a later tuner knows.
--
-- Values are the current code defaults, so this migration changes NO behaviour.
-- It moves the dial from source to data, the same way migration 150 did for
-- grade_scope, grade_chunk_min_ratio and topical_admission.
--
-- Applied to EVERY active settings row: config is per legal person (global plus
-- three schools today) and a knob added to one row leaves the rest on the old
-- default with no error anywhere.

SET search_path TO edu_ai, public;

BEGIN;

UPDATE settings s
   SET config = jsonb_set(
         s.config::jsonb, '{pipeline}',
         (SELECT jsonb_agg(
                   CASE WHEN x->>'step' = 'answer'
                     THEN jsonb_set(
                            jsonb_set(x, '{params,attrib_positional_min}',
                                      '0.15'::jsonb, true),
                            '{params,attrib_best_min}', '0.25'::jsonb, true)
                     ELSE x END ORDER BY ord)
            FROM jsonb_array_elements(s.config::jsonb->'pipeline')
                 WITH ORDINALITY AS t(x, ord))
       )::json
 WHERE s.is_active;

DO $$
DECLARE bad int;
BEGIN
    SELECT count(*) INTO bad
      FROM settings s, jsonb_array_elements(s.config::jsonb->'pipeline') AS x
     WHERE s.is_active AND x->>'step' = 'answer'
       AND ((x->'params'->>'attrib_positional_min') IS NULL
         OR (x->'params'->>'attrib_best_min') IS NULL);
    IF bad > 0 THEN
        RAISE EXCEPTION '% active row(s) missing the attribution thresholds', bad;
    END IF;
    -- the invariant, checked once here so a bad seed fails loudly
    SELECT count(*) INTO bad
      FROM settings s, jsonb_array_elements(s.config::jsonb->'pipeline') AS x
     WHERE s.is_active AND x->>'step' = 'answer'
       AND (x->'params'->>'attrib_best_min')::numeric
           <= (x->'params'->>'attrib_positional_min')::numeric;
    IF bad > 0 THEN
        RAISE EXCEPTION 'attrib_best_min must stay above attrib_positional_min (% row(s))', bad;
    END IF;
END $$;

COMMIT;
