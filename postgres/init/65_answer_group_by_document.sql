-- last change in code by Martin Safranek (2026-08-01).
-- 65_answer_group_by_document.sql
-- search_answer_generation v2: when the context covers more than one topic, the
-- answer is split into paragraphs, each opened by a few words naming what it is
-- about.
--
-- What went wrong. "jaké potřebuji pomůcky?" retrieved two unrelated things — the
-- CERMAT rules for what may be brought INTO the entrance exam, and the school's own
-- supply lists per grade — and the model blended them into one paragraph: a
-- permitted exam pen and tempera paints in the same breath. Both halves were
-- correct and grounded; the answer was still misleading, because the reader cannot
-- tell that the first sentence is about an exam room and the second about buying
-- school things in September.
--
-- The context now arrives grouped by document (answer.py sorts by document, then
-- section, then chunk_index) and every chunk is introduced by
-- "| section: <heading> > <subheading>", so the model has what it needs to keep
-- the groups apart. This prompt tells it to.
--
-- The length cap becomes per-group. A single-topic answer stays as short as before;
-- a two-topic answer is allowed the second paragraph rather than being squeezed
-- into 120 words, which is what forced the blending in the first place.
--
-- The lead-in is prose, not a citation: "Při přijímacích zkouškách:" is a topic
-- label, not a file name — the source-citation rules are unchanged, the
-- application still renders the documents as links.
--
-- v1 is deactivated, not deleted (prompt_registry_one_active allows one active row
-- per key). Idempotent; transactional.

BEGIN;

-- prompt_registry_one_active is a UNIQUE partial index on (prompt_key) WHERE
-- is_active, so the old version has to step aside BEFORE the new one is inserted —
-- inside this transaction, so a failure leaves exactly one active row either way.
UPDATE edu_ai.prompt_registry
   SET is_active = false
 WHERE prompt_key = 'search_answer_generation'
   AND is_active
   AND NOT EXISTS (
       SELECT 1 FROM edu_ai.prompt_registry
        WHERE prompt_key = 'search_answer_generation'
          AND prompt_text LIKE '%GROUPING%');

INSERT INTO edu_ai.prompt_registry
    (prompt_key, version, prompt_text, is_active, component, description,
     model_hint, lang, created_by, notes)
SELECT
    'search_answer_generation',
    (SELECT max(version) + 1 FROM edu_ai.prompt_registry
      WHERE prompt_key = 'search_answer_generation'),
    replace(r.prompt_text,
        E'---\\n\\nLENGTH\\n\\nBe concise. Answer in the language of the question, in at most ~120 words\\n(a short paragraph or a compact list). Do not restate the question or\\nsummarize the context; give the answer directly.',
        E'---\\n\\nGROUPING\\n\\nThe context is grouped by document, and each chunk is introduced by a line\\nholding its section heading ("| section: …"). Use that structure:\\n\\n1. If the context answers the question from ONE topic, write one paragraph.\\n2. If it covers DISTINCT situations — a different document, or clearly different\\n   circumstances — write ONE paragraph PER situation, in the order they appear in\\n   the context, and open each with a few words naming the situation, followed by\\n   a colon. Take the naming from the section headings, in the user''s language.\\n   Example shape: "Při přijímacích zkouškách: … / Pro školní rok na základní\\n   škole: …"\\n3. Never merge two situations into one sentence. A reader must be able to tell\\n   which circumstance each statement applies to.\\n4. At most 3 paragraphs. If more situations are present, cover the ones the\\n   question is most likely about and say that the rest exist.\\n5. The lead-in is a topic label in prose — never a file name, a uuid or a\\n   "(Document N)" tag.\\n\\n---\\n\\nLENGTH\\n\\nBe concise. Answer in the language of the question, in at most ~120 words per\\nparagraph (a short paragraph or a compact list each). Do not restate the\\nquestion or summarize the context; give the answer directly.'),
    true, r.component,
    'Grounded answer generation; v2 groups the answer per topic/document',
    r.model_hint, r.lang, 'migration 65',
    'v2: answers spanning several documents are split into one paragraph per '
    || 'situation with a short topic lead-in. See migration 65 for the case that '
    || 'prompted it ("jaké potřebuji pomůcky?" blending exam rules with school '
    || 'supply lists).'
FROM edu_ai.prompt_registry r
WHERE r.prompt_key = 'search_answer_generation'
  AND r.version = (SELECT max(version) FROM edu_ai.prompt_registry
                    WHERE prompt_key = 'search_answer_generation')
  AND NOT EXISTS (
      SELECT 1 FROM edu_ai.prompt_registry
       WHERE prompt_key = 'search_answer_generation'
         AND prompt_text LIKE '%GROUPING%');

COMMIT;
