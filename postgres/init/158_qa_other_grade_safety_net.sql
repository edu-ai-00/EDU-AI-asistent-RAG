-- last change in code by Martin Safranek (2026-08-20).
--
-- 158 — the two knobs of the other-ročník safety net (qa_lexical).
--
-- A pack of the caller's OWN school that the ročník filter excluded may never
-- answer: a curated answer is returned verbatim, so a ročník-9 rule handed to a
-- ročník-6 pupil is a wrong statement nothing downstream can qualify. It is now
-- OFFERED as a suggestion instead of vanishing — dropping it silently takes a
-- mistagged pack's whole content out of reach with nothing to say it existed,
-- and once schools tag their own packs that becomes the common failure.
--
--   other_grade_top_n      2    how many such questions are offered
--   other_grade_min_ratio  0.5  relative floor on ts_rank against the best of
--                               them. The net's query is a plain OR over the
--                               query tokens (no synonyms, no coverage — the bar
--                               for OFFERING is "the words match"), so one shared
--                               common word ranks non-zero. Measured on
--                               "telefony ve škole?": the second offer was "Do kdy
--                               musím nahlásit nepřítomnost?", matched on "škole".
--
-- A sibling unit's pack is NOT offered, by construction in route._scope(): in this
-- corpus the SOŠ answer to the phone question is the opposite of the ZŠ one
-- (breaks allowed vs forbidden), so showing it to a ZŠ pupil beats no button only
-- in the sense of being worse.

BEGIN;

UPDATE edu_ai.settings s
   SET config = jsonb_set(s.config::jsonb, '{pipeline}', (
         SELECT jsonb_agg(
                  CASE WHEN e.elem ->> 'step' <> 'qa_lexical' THEN e.elem
                       ELSE jsonb_set(e.elem, '{params}',
                              CASE WHEN jsonb_typeof(e.elem -> 'params') = 'object'
                                   THEN e.elem -> 'params' ELSE '{}'::jsonb END
                              || jsonb_build_object('other_grade_top_n', 2,
                                                    'other_grade_min_ratio', 0.5))
                  END ORDER BY e.ord)
           FROM jsonb_array_elements(s.config::jsonb -> 'pipeline')
                WITH ORDINALITY AS e(elem, ord)
       ))::json
 WHERE s.is_active
   AND jsonb_typeof(s.config::jsonb -> 'pipeline') = 'array'
   AND jsonb_array_length(s.config::jsonb -> 'pipeline') > 0;

DO $$
DECLARE n int;
BEGIN
    SELECT count(*) INTO n
      FROM edu_ai.settings s, jsonb_array_elements(s.config::jsonb->'pipeline') e
     WHERE s.is_active AND e->>'step' = 'qa_lexical'
       AND NOT (e -> 'params' ?& array['other_grade_top_n','other_grade_min_ratio']);
    IF n > 0 THEN
        RAISE EXCEPTION '158: keys missing on % qa_lexical step(s)', n;
    END IF;
    RAISE NOTICE '158: other-ročník safety net configurable on every active row';
END $$;

COMMIT;
