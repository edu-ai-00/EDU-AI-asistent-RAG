-- last change in code by Martin Safranek (2026-07-28).
-- 36_prompt_v5_no_language.sql
-- Language leaves the LLM entirely (agreed 2026-07-28): its identification is
-- deterministic end-to-end (user code > shared detect_lang — the same function
-- that already selects the reduction engines), so the classification prompt
-- stops deriving document_language. prompt v5 = v4 minus:
--   * "- document_language" in the TASK list
--   * the whole LANGUAGE section ({{LANGUAGES}} placeholder)
--   * "document_language" in the OUTPUT keys
-- The LLM keeps ONLY what has no deterministic substitute: document_type,
-- school_subject, school_grade — and per RFP it runs only when the user did
-- not supply them (the skip itself is component logic, not SQL).
-- Derived from v4 via replace() so formatting stays consistent. Idempotent.

BEGIN;

INSERT INTO edu_ai.prompt_registry
    (prompt_key, version, prompt_text, is_active, component, description,
     placeholders, model_hint, lang, created_by, notes)
SELECT prompt_key, 5,
       replace(replace(replace(prompt_text,
E'- document_language\n', ''),
E'LANGUAGE codes (ISO 639-1):\n{{LANGUAGES}}\nReturn the code of the DOMINANT language of the text — exactly one of the codes above.\n\n', ''),
'document_type, classification_confidence, document_language, school_subject, school_grade',
'document_type, classification_confidence, school_subject, school_grade'),
       false, component,
       'v5: language removed from LLM classification — identified deterministically (user code > shared detect_lang); LLM keeps type/subject/grade only.',
       ARRAY['DOCUMENT_TYPES','SCHOOL_SUBJECTS','SCHOOL_GRADES']::text[],
       model_hint, lang, 'migration-36',
       'Derived from v4; v4 kept inactive for rollback.'
FROM edu_ai.prompt_registry
WHERE prompt_key = 'document_metadata_extraction' AND version = 4
  AND NOT EXISTS (SELECT 1 FROM edu_ai.prompt_registry
                  WHERE prompt_key = 'document_metadata_extraction' AND version = 5);

UPDATE edu_ai.prompt_registry SET is_active = false
WHERE prompt_key = 'document_metadata_extraction' AND version < 5 AND is_active;
UPDATE edu_ai.prompt_registry SET is_active = true
WHERE prompt_key = 'document_metadata_extraction' AND version = 5 AND NOT is_active;

COMMIT;
