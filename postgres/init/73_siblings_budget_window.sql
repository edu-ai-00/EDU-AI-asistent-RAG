-- last change in code by Martin Safranek (2026-08-02).
-- 73_siblings_budget_window.sql
-- document_siblings: the reach follows the per-element budget instead of a
-- separate fixed window.
--
-- Before: max_chunks_per_element = 6 decided only WHETHER an element was capped,
-- and a capped element then contributed the match plus window = 1 on each side —
-- three chunks. So an element with 6 chunks came in whole while one with 7 gave
-- three, and the budget of 6 was never actually spent on the elements it was meant
-- to bound. The step on the current corpus: 63 elements are under the cap (218
-- chunks) and 12 are over it (183 chunks, the largest 31).
--
-- Now max_chunks_per_element is a BUDGET for the element, counting the matched
-- chunks. Below it the whole section still comes in. Above it, chunks are taken in
-- order of distance from the nearest match — immediate neighbours, then the next
-- ring — until the budget is spent, so with a budget of 6 and one match a long
-- element contributes roughly two chunks on each side rather than one.
--
-- window changes meaning: it is now an optional HARD cap on that distance, for a
-- corpus where a far chunk is never wanted. 0 (set here) means the budget is the
-- only limit — keeping it at 1 would reproduce the old three-chunk behaviour and
-- make the budget change invisible.
--
-- Idempotent; transactional.

BEGIN;

UPDATE edu_ai.search_setting s
   SET config = jsonb_set(s.config, '{pipeline}', (
         SELECT jsonb_agg(CASE
                  WHEN step->>'step' = 'document_siblings'
                  THEN jsonb_set(step, '{params}',
                         coalesce(step->'params', '{}'::jsonb)
                         || jsonb_build_object('window', 0), true)
                  ELSE step END ORDER BY ord)
         FROM jsonb_array_elements(s.config->'pipeline') WITH ORDINALITY e(step, ord)
       ), false)
 WHERE s.config->'pipeline' @> '[{"step": "document_siblings"}]'::jsonb;

UPDATE edu_ai.search_setting s
   SET config = jsonb_set(s.config, '{pipeline}', (
         SELECT jsonb_agg(CASE
                  WHEN step->>'step' = 'document_siblings'
                  THEN jsonb_set(step, '{note}', to_jsonb(
                         'Completes a matched section with its remaining chunks '
                      || '(same file_item_uuid). Context, not evidence: siblings '
                      || 'get no score or coverage. max_chunks_per_element is a '
                      || 'BUDGET per element including the matched chunks — under '
                      || 'it the whole section comes in, over it chunks are taken '
                      || 'outwards from the match until it is spent. window is an '
                      || 'optional hard cap on that distance; 0 = budget only.'), true)
                  ELSE step END ORDER BY ord)
         FROM jsonb_array_elements(s.config->'pipeline') WITH ORDINALITY e(step, ord)
       ), false)
 WHERE s.config->'pipeline' @> '[{"step": "document_siblings"}]'::jsonb;

COMMIT;
