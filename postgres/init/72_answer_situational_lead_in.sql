-- last change in code by Martin Safranek (2026-08-02).
-- 72_answer_situational_lead_in.sql
-- search_answer_generation v4: the paragraph lead-in names the SITUATION, not the
-- document's section.
--
-- v2 told the model to "take the naming from the section headings". It obeyed, and
-- the reader got:
--
--     **8. Pravidla, povolené pomůcky a den D:** …
--     **Seznam pomůcek na školní rok 2025/2026 - 1. stupeň; Seznam pomůcek na
--       školní rok 2026/2027 - 2. stupeň:** …
--
-- The "8." is that article's internal numbering — the reader has never seen its
-- table of contents, so the number tells them nothing and only leaks the source's
-- structure. "a den D" is the article's editorial voice. And a document that
-- contributed several sections produced a semicolon-glued list of all of them.
--
-- The label has to answer WHEN OR WHERE this applies to the reader: "Při
-- přijímacích zkouškách:", "Během školního roku:". So the instruction becomes
-- write, not copy — the headings are information about the content, never text to
-- reuse. Measured after (gpt-4.1-nano, twice per query, stable):
--
--     "jaké potřebuji pomůcky?"        Při přípravě na den D / Při jednotných
--                                      testech / Na začátku školního roku
--     "…pomůcky do 7. třídy?"          Při výuce na 2. stupni / Při přijímacích
--                                      zkouškách
--
-- The concrete per-request shape is generated in code (answer.py knows how many
-- documents there are and what each covers); this keeps the system prompt from
-- contradicting it.
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
          AND prompt_text LIKE '%WHEN OR WHERE this applies%');

INSERT INTO edu_ai.prompt_registry
    (prompt_key, version, prompt_text, is_active, component, description,
     model_hint, lang, created_by, notes)
SELECT
    'search_answer_generation',
    (SELECT max(version) + 1 FROM edu_ai.prompt_registry
      WHERE prompt_key = 'search_answer_generation'),
    replace(r.prompt_text,
      E'   a colon. Take the naming from the section headings, in the user''s language.\n   Example shape: "Při přijímacích zkouškách: … / Pro školní rok na základní\n   škole: …"',
      E'   a colon. WRITE that label in 2-5 words of your own — it answers WHEN OR WHERE\n   this applies to the reader, in their language, never what the document is\n   called. A section heading is information about the content, not text to reuse,\n   and a document''s section numbering or internal structure never belongs in it.\n   Example shape: "Při přijímacích zkouškách: … / Během školního roku: …"'),
    true, r.component,
    'Grounded answer generation; v4 situational paragraph lead-in',
    r.model_hint, r.lang, 'migration 72',
    'v4: the lead-in names the situation for the reader instead of copying the '
    || 'document''s section heading — see migration 72 for what the copy looked like.'
FROM edu_ai.prompt_registry r
WHERE r.prompt_key = 'search_answer_generation'
  AND r.version = (SELECT max(version) FROM edu_ai.prompt_registry
                    WHERE prompt_key = 'search_answer_generation')
  AND NOT EXISTS (
      SELECT 1 FROM edu_ai.prompt_registry
       WHERE prompt_key = 'search_answer_generation'
         AND prompt_text LIKE '%WHEN OR WHERE this applies%');

COMMIT;
