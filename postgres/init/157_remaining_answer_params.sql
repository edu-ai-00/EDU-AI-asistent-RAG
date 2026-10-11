-- last change in code by Martin Safranek (2026-08-20).
--
-- 157 — the last three `answer` knobs, and a correction to 154's own reasoning.
--
-- Migration 154 left these out claiming "the code has no default". That was
-- wrong: all three DO have one, written in a shape my extraction did not match
-- (`x if v is None else`, and `or N`), not as `params.get(key, default)`.
--
--   clarify_max_match_score  1.0  — above this match score the answer is treated
--                                   as confident and no clarification is offered.
--                                   1.0 = never suppressed by score.
--   related_suggestions_top_n  3  — how many related sections are offered.
--   log_candidates_n          20  — how many candidates of the fused pool are
--                                   written to the query log, with every
--                                   per-signal rank, so the log can show where
--                                   the expected chunk landed and what outranked it.
--
-- Same rule as 154: the value equals the code's default, so nothing changes.
--
-- Also in this step (code, not data): the `rerank` bool + `rerank_backend` pair
-- was deleted from answer.py. It was a second spelling of `rerank_mode` kept for
-- backward compatibility, and nothing set it — no settings row, no script, no
-- request. Two names for one knob is a trap, not a feature.

BEGIN;

UPDATE edu_ai.settings s
   SET config = jsonb_set(s.config::jsonb, '{pipeline}', (
         SELECT jsonb_agg(
                  CASE WHEN e.elem ->> 'step' <> 'answer' THEN e.elem
                       ELSE jsonb_set(e.elem, '{params}',
                              CASE WHEN jsonb_typeof(e.elem -> 'params') = 'object'
                                   THEN e.elem -> 'params' ELSE '{}'::jsonb END
                              || jsonb_build_object(
                                   'clarify_max_match_score', 1.0,
                                   'related_suggestions_top_n', 3,
                                   'log_candidates_n', 20))
                  END ORDER BY e.ord)
           FROM jsonb_array_elements(s.config::jsonb -> 'pipeline')
                WITH ORDINALITY AS e(elem, ord)
       ))::json
 WHERE s.is_active
   AND jsonb_typeof(s.config::jsonb -> 'pipeline') = 'array'
   AND jsonb_array_length(s.config::jsonb -> 'pipeline') > 0;

DO $$
DECLARE
    n int;
BEGIN
    SELECT count(*) INTO n
      FROM edu_ai.settings s, jsonb_array_elements(s.config::jsonb->'pipeline') e
     WHERE s.is_active AND e->>'step' = 'answer'
       AND NOT (e -> 'params' ?& array['clarify_max_match_score',
                'related_suggestions_top_n','log_candidates_n']);
    IF n > 0 THEN
        RAISE EXCEPTION '157: keys missing on % answer step(s)', n;
    END IF;

    -- the removed aliases must not linger anywhere
    SELECT count(*) INTO n
      FROM edu_ai.settings s, jsonb_array_elements(s.config::jsonb->'pipeline') e
     WHERE (e -> 'params') ?| array['rerank','rerank_backend'];
    IF n > 0 THEN
        RAISE EXCEPTION '157: % row(s) still carry the removed rerank aliases', n;
    END IF;

    RAISE NOTICE '157: 3 parameters added; rerank aliases confirmed absent';
END $$;

COMMIT;
