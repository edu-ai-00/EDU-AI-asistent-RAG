-- last change in code by Martin Safranek (2026-08-05).
-- 125_doc_metadata_prompt_all.sql
-- document_metadata_extraction v2: the grade instruction predates the ALL
-- value (init 121) — the model only knew "a specific grade or UNKNOWN", so a
-- school code that applies to everyone got no grade at all. v2 teaches it
-- when ALL is the right answer. ({{SCHOOL_GRADES}} itself is now substituted
-- from the ENTITY'S resolved codebook — BatchRun r6dJ7 — so ALL appears in
-- the offered list exactly where the school defines it.)
--
-- Idempotent; transactional.

BEGIN;

UPDATE edu_ai.prompt_registry SET is_active = false
 WHERE prompt_key = 'document_metadata_extraction' AND version = 1;

INSERT INTO edu_ai.prompt_registry
    (prompt_key, version, prompt_text, is_active, component, description,
     placeholders, model_hint, lang, created_by, notes)
SELECT 'document_metadata_extraction', 2,
       replace(p.prompt_text,
'Use a grade code only when the grade is explicitly stated or clearly inferable from the text; otherwise UNKNOWN.',
'Use ALL when the document applies to every grade or the school as a whole (a school code, canteen rules, general guidelines for everyone). Use a specific grade code only when the grade is explicitly stated or clearly inferable from the text. Otherwise UNKNOWN.'),
       true, p.component,
       'Document-level metadata classification v2: knows the ALL ("Pro všechny ročníky") grade value.',
       p.placeholders, p.model_hint, p.lang, 'init-125',
       'v2: grade instruction covers ALL (init 121); {{SCHOOL_GRADES}} substitution is entity-resolved in the component.'
FROM edu_ai.prompt_registry p
WHERE p.prompt_key = 'document_metadata_extraction' AND p.version = 1
ON CONFLICT (prompt_key, version) DO NOTHING;

COMMIT;
