-- last change in code by Martin Safranek (2026-08-01).
-- 59_document_siblings.sql
-- New pipeline step document_siblings: complete a matched section from its own
-- chunks, at query time.
--
-- Why: a page element (one heading + its text) is split into several chunks
-- sharing a file_item_uuid, and only the FIRST carries the heading. "Seznam
-- pomůcek na školní rok 2026/2027" is chunk 0 of its element; chunks 1 and 2 are
-- bare lists ("4x 544, 2x 460, … vodovky, tempery") that match nothing —
-- measured coverage 0.000 and cosine rank 25/503. The section was findable
-- through one chunk and unreadable through the rest.
--
--   max_chunks_per_element  above this, a matched section is completed only
--                           AROUND the matched chunk, not wholly. Elements here
--                           run 1..29 chunks (up to 22 kB); pulling the largest
--                           in whole would spend two thirds of the context
--                           budget on a single match.
--   window                  how many chunks either side of a match to take in
--                           that case.
--
-- Siblings carry no score, coverage or rank: they are CONTEXT, invited by a
-- match rather than matched themselves. Counting them as evidence would let a
-- long section win by being long.
--
-- Placed after the gatherers and before `answer`, so it completes whatever the
-- lexical/keyword/vector steps chose.
--
-- Idempotent (skips when the step is already in the pipeline); transactional.

BEGIN;

UPDATE edu_ai.search_setting s
   SET config = jsonb_set(s.config, '{pipeline}', (
         SELECT jsonb_agg(x ORDER BY ord)
         FROM (
           SELECT step AS x, ord * 10 AS ord
             FROM jsonb_array_elements(s.config->'pipeline') WITH ORDINALITY e(step, ord)
           UNION ALL
           SELECT jsonb_build_object(
                    'step', 'document_siblings',
                    'on_hit', 'continue',
                    'enabled', true,
                    'note', 'Completes a matched section with its remaining chunks '
                         || '(same file_item_uuid). Context, not evidence: siblings '
                         || 'get no score or coverage. A section larger than '
                         || 'max_chunks_per_element is completed only within '
                         || 'window chunks of the match.',
                    'params', jsonb_build_object('max_chunks_per_element', 6,
                                                 'window', 1)),
                  (SELECT ord * 10 - 5
                     FROM jsonb_array_elements(s.config->'pipeline') WITH ORDINALITY e(step, ord)
                    WHERE step->>'step' = 'answer')
         ) q), false)
 WHERE NOT (s.config->'pipeline' @> '[{"step": "document_siblings"}]'::jsonb)
   AND s.config->'pipeline' @> '[{"step": "answer"}]'::jsonb;

COMMIT;
