-- last change in code by Martin Safranek (2026-08-01).
-- 67_answer_document_groups.sql
-- Two parameters for the answer built per document.
--
-- max_chunks_per_document — the context is now assembled in two phases: the
-- content_top_n best-ranked chunks are SEEDS that decide which documents the
-- answer is built from and in what order (a document's position is the position of
-- its best chunk), and then each of those documents is filled in from the chunks
-- of it that are already in the shortlist. Without a per-document cap one long
-- document would take the whole budget; 6 matches document_siblings'
-- max_chunks_per_element, which bounds the same risk one level down.
--
-- max_tokens 350 -> 500 — a two-document answer is two paragraphs, and 350 output
-- tokens cut the second one short. Single-document answers are unaffected: the
-- model is asked for at most ~120 words per paragraph either way, so the cap only
-- stops being the binding constraint when there really are two topics.
--
-- What made this necessary. "jaké potřebuji pomůcky?" retrieves two unrelated
-- documents — the CERMAT rules for what may be taken INTO the entrance exam, and
-- the school's own supply lists per grade. Interleaved by rank, they produced one
-- paragraph attributing exam answer-sheet rules to elementary school. Both halves
-- were grounded; the answer was still wrong, because the reader could not tell
-- which situation each sentence belonged to.
--
-- The grouping instruction itself is NOT here. Asking for it in the system prompt
-- (migration 65) was ignored by gpt-4.1-nano AND gpt-4.1-mini — both wrote one
-- paragraph and silently dropped a document, which is worse than a long answer. It
-- is generated in code instead, at the END of the user message, where the language
-- rule already lives for the same measured reason: answer.py knows how many
-- documents are in the context and which sections of each, so it states the shape
-- ("write exactly 2 paragraphs, in this order: 1) … 2) …") rather than asking for
-- it. That is what made it work, on nano, repeatably.
--
-- Idempotent; transactional.

BEGIN;

UPDATE edu_ai.search_setting s
   SET config = jsonb_set(s.config, '{pipeline}', (
         SELECT jsonb_agg(CASE
                  WHEN step->>'step' = 'answer'
                  THEN jsonb_set(step, '{params}',
                         coalesce(step->'params', '{}'::jsonb)
                         || jsonb_build_object('max_chunks_per_document', 6,
                                               'max_tokens', 500),
                         true)
                  ELSE step END ORDER BY ord)
         FROM jsonb_array_elements(s.config->'pipeline') WITH ORDINALITY e(step, ord)
       ), false)
 WHERE s.config->'pipeline' @> '[{"step": "answer"}]'::jsonb
   AND NOT (s.config #> '{pipeline}'
            @> '[{"params": {"max_chunks_per_document": 6}}]'::jsonb);

COMMIT;
