-- last change in code by Martin Safranek (2026-08-02).
-- 69_answer_grounding_and_tokens.sql
-- search_answer_generation v3 + max_tokens 900.
--
-- Raising max_tokens alone did NOT do what it was asked to do. Measured on
-- "jaké potřebuji pomůcky?" with three documents in the context:
--
--     max_tokens 500   995 chars, no concrete list
--     max_tokens 900   995 chars, no concrete list
--     max_tokens 1400 1028 chars, no concrete list
--
-- The output cap was never the binding constraint — the prompt's own "~120 words
-- per paragraph" was, and the model obeyed it by paraphrasing the school's supply
-- list into categories ("various kinds of paper"). For a list, that paraphrase
-- destroys the answer: the list IS the answer.
--
-- And giving the model more room made it worse, not better. At 1400 tokens it
-- wrote, for the first grade, "psací potřeby, sešity, pastelky, lepidlo, nůžky" —
-- while the entire source chunk for that section reads:
--
--     Seznam pomůcek na školní rok 2025/26 / 1. třída
--     * Podle informací na červnové třídní schůzce
--
-- There is no list there at all. The items were invented, some of them borrowed
-- from the OTHER document's prohibition list. More space to fill turned into more
-- fabrication, which is the one failure mode a school assistant must not have.
--
-- So v3 does two things instead: it forbids illustrative examples that are not in
-- the context (a section that only points elsewhere must be reported as such, and
-- list nothing), and it exempts a paragraph that reproduces a list from the length
-- guidance. Verified after: the 2nd-stage answer carries "4x 544, 2x 460, 1x 540,
-- 9x 564 …" verbatim and the first-grade paragraph says only that the information
-- came from the June parents' meeting.
--
-- max_tokens still goes 500 -> 900: three grounded paragraphs, one of them a real
-- list, need the headroom (1181 chars measured), and 500 was cutting the third.
--
-- Idempotent; transactional.

BEGIN;

UPDATE edu_ai.prompt_registry
   SET is_active = false
 WHERE prompt_key = 'search_answer_generation'
   AND is_active
   AND NOT EXISTS (
       SELECT 1 FROM edu_ai.prompt_registry
        WHERE prompt_key = 'search_answer_generation'
          AND prompt_text LIKE '%NO ILLUSTRATIVE EXAMPLES%');

INSERT INTO edu_ai.prompt_registry
    (prompt_key, version, prompt_text, is_active, component, description,
     model_hint, lang, created_by, notes)
SELECT
    'search_answer_generation',
    (SELECT max(version) + 1 FROM edu_ai.prompt_registry
      WHERE prompt_key = 'search_answer_generation'),
    r.prompt_text || E'\n\n---\n\nGROUNDING — NO ILLUSTRATIVE EXAMPLES\n\n'
      'Never write "for example" followed by items that are not in the context. If '
      'a section only refers the reader elsewhere (a meeting, another page) and '
      'lists nothing, say exactly that and list nothing. Items from one section '
      'must never be presented as belonging to another.\n'
      'When the context DOES list concrete items, reproduce them as they stand — a '
      'list is the answer, and paraphrasing it into categories ("various kinds of '
      'paper") destroys it. A paragraph reproducing a list may exceed the length '
      'guidance.',
    true, r.component,
    'Grounded answer generation; v3 forbids invented examples and preserves lists',
    r.model_hint, r.lang, 'migration 69',
    'v3: no illustrative examples outside the context; a list is reproduced '
    || 'verbatim and is exempt from the per-paragraph length guidance. Prompted by '
    || 'a fabricated first-grade supply list at max_tokens 1400 — see migration 69.'
FROM edu_ai.prompt_registry r
WHERE r.prompt_key = 'search_answer_generation'
  AND r.version = (SELECT max(version) FROM edu_ai.prompt_registry
                    WHERE prompt_key = 'search_answer_generation')
  AND NOT EXISTS (
      SELECT 1 FROM edu_ai.prompt_registry
       WHERE prompt_key = 'search_answer_generation'
         AND prompt_text LIKE '%NO ILLUSTRATIVE EXAMPLES%');

UPDATE edu_ai.search_setting s
   SET config = jsonb_set(s.config, '{pipeline}', (
         SELECT jsonb_agg(CASE
                  WHEN step->>'step' = 'answer'
                  THEN jsonb_set(step, '{params}',
                         coalesce(step->'params', '{}'::jsonb)
                         || jsonb_build_object('max_tokens', 900), true)
                  ELSE step END ORDER BY ord)
         FROM jsonb_array_elements(s.config->'pipeline') WITH ORDINALITY e(step, ord)
       ), false)
 WHERE s.config->'pipeline' @> '[{"step": "answer"}]'::jsonb;

COMMIT;
