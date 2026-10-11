-- last change in code by Martin Safranek (2026-07-28).
-- 39_prompts_language_neutral.sql
-- Remove the remaining Czech-specific wording from the four active prompts that
-- still name the language. None of them REQUIRED Czech (that would have broken
-- English ingestion outright); they carried Czech as an example or default:
--   * chunk_keyword_extraction / table_metadata_extraction: "keep Czech
--     diacritics" — harmless for English (no diacritics) but wrong the moment a
--     third language arrives, and it implies a language the LLM should not assume
--   * search_answer_generation: the LANGUAGE RULE enumerated only cs/en; the
--     actual answer language is already forced deterministically by the
--     "Respond in <language>" instruction appended per request (answer.py)
--   * table_metadata_validator: "any language (including Czech)" — the "any" was
--     already correct, the parenthesis just anchors it to Czech
-- New versions (version+1, previous deactivated) — prompts are read per request,
-- so no code change and no restart. Idempotent per prompt_key.

BEGIN;

-- 1) keyword + table metadata extraction: diacritics instruction generalized
INSERT INTO edu_ai.prompt_registry
    (prompt_key, version, prompt_text, is_active, component, description,
     placeholders, model_hint, lang, created_by, notes)
SELECT prompt_key, version + 1,
       replace(prompt_text, 'keep Czech diacritics',
                            'keep the diacritics of the source language'),
       false, component,
       'Language-neutral: diacritics instruction no longer names Czech.',
       placeholders, model_hint, 'multi', 'migration-39',
       'Derived from v' || version || '; previous version kept inactive for rollback.'
FROM edu_ai.prompt_registry p
WHERE is_active
  AND prompt_key IN ('chunk_keyword_extraction', 'table_metadata_extraction')
  AND prompt_text LIKE '%keep Czech diacritics%'
  AND NOT EXISTS (SELECT 1 FROM edu_ai.prompt_registry x
                  WHERE x.prompt_key = p.prompt_key AND x.created_by = 'migration-39');

-- 2) table metadata validator: drop the Czech anchor from "any language"
INSERT INTO edu_ai.prompt_registry
    (prompt_key, version, prompt_text, is_active, component, description,
     placeholders, model_hint, lang, created_by, notes)
SELECT prompt_key, version + 1,
       replace(prompt_text, 'Text may be in any language (including Czech).',
                            'Text may be in any language.'),
       false, component,
       'Language-neutral: "any language" no longer anchored to Czech.',
       placeholders, model_hint, 'multi', 'migration-39',
       'Derived from v' || version || '; previous version kept inactive for rollback.'
FROM edu_ai.prompt_registry p
WHERE is_active AND prompt_key = 'table_metadata_validator'
  AND prompt_text LIKE '%including Czech%'
  AND NOT EXISTS (SELECT 1 FROM edu_ai.prompt_registry x
                  WHERE x.prompt_key = p.prompt_key AND x.created_by = 'migration-39');

-- 3) answer generation: LANGUAGE RULE stated generically (the per-request
--    "Respond in <language>" instruction is what actually enforces it)
INSERT INTO edu_ai.prompt_registry
    (prompt_key, version, prompt_text, is_active, component, description,
     placeholders, model_hint, lang, created_by, notes)
SELECT prompt_key, version + 1,
       replace(replace(prompt_text,
           '   → If the question is in English → answer in English (even if context is in Czech or other language)',
           '   → Answer in the question''s language even when the context documents are in a different language'),
           '   → If the question is in Czech → answer in Czech (even if context is in English)',
           '   → A trailing instruction states the required answer language explicitly — follow it exactly'),
       false, component,
       'Language-neutral LANGUAGE RULE (the deterministic per-request "Respond in <language>" instruction enforces it).',
       placeholders, model_hint, 'multi', 'migration-39',
       'Derived from v' || version || '; previous version kept inactive for rollback.'
FROM edu_ai.prompt_registry p
WHERE is_active AND prompt_key = 'search_answer_generation'
  AND prompt_text LIKE '%answer in Czech%'
  AND NOT EXISTS (SELECT 1 FROM edu_ai.prompt_registry x
                  WHERE x.prompt_key = p.prompt_key AND x.created_by = 'migration-39');

-- activate the new versions (one active row per key)
UPDATE edu_ai.prompt_registry SET is_active = false
WHERE is_active AND prompt_key IN (
    SELECT prompt_key FROM edu_ai.prompt_registry WHERE created_by = 'migration-39');
UPDATE edu_ai.prompt_registry SET is_active = true
WHERE created_by = 'migration-39' AND NOT is_active;

COMMIT;
