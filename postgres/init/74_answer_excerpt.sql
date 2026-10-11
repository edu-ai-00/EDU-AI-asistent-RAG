-- last change in code by Martin Safranek (2026-08-02).
-- 74_answer_excerpt.sql
-- Each answer paragraph can carry the source text it was written from.
--
-- answer_parts already tells a client which document a paragraph came from, so the
-- link can sit under the paragraph. What it could not do is SHOW what the paragraph
-- was actually built from — the reader had to follow the link to a whole page and
-- find the passage themselves. `excerpt` closes that: the chunks of that document
-- which were in the context, in the order the model read them, each introduced by
-- its heading.
--
-- The headings are written into the excerpt on purpose. They are not part of
-- chunk_text since migration 61, and without them a chunk reading
-- "4x 544, 2x 460, 1x 540, 9x 564 …" is unreadable on its own.
--
-- Opt-in, because it roughly doubles the response: the context is up to
-- token_budget * 4 characters and this returns it a second time, sliced per
-- document. A client that offers no "show the source" affordance should turn it
-- off — a mobile client on a slow link pays for bytes it will never render.
--
--     include_excerpt     true   return it
--     excerpt_max_chars   4000   per document, cut on a whitespace boundary and
--                                marked with […]
--
-- Idempotent; transactional.

BEGIN;

UPDATE edu_ai.search_setting s
   SET config = jsonb_set(s.config, '{pipeline}', (
         SELECT jsonb_agg(CASE
                  WHEN step->>'step' = 'answer'
                  THEN jsonb_set(step, '{params}',
                         coalesce(step->'params', '{}'::jsonb)
                         || jsonb_build_object('include_excerpt', true,
                                               'excerpt_max_chars', 4000), true)
                  ELSE step END ORDER BY ord)
         FROM jsonb_array_elements(s.config->'pipeline') WITH ORDINALITY e(step, ord)
       ), false)
 WHERE s.config->'pipeline' @> '[{"step": "answer"}]'::jsonb
   AND NOT (s.config #> '{pipeline}'
            @> '[{"params": {"include_excerpt": true}}]'::jsonb);

COMMIT;
