-- last change in code by Martin Safranek (2026-08-04).
-- 117_table_enrichment.sql
-- The TABLE branch joins the unification (init 116): table_metadata_extraction
-- already chunks a table AND extracts keywords in one call — v2 adds grades
-- and subjects per chunk to that same call (appended STEP 3, the v1 body is
-- kept verbatim). ParseLLMChunkJSON serializes them into the
-- llm_grades/llm_subjects columns and the insert component validates and
-- unions them against the school's číselník, its own second LLM pass standing
-- down (column-presence marker, same mechanism as the text branch).
-- chunk_grade_subject_extraction stays active only as the fallback when the
-- combined output is missing (parse failure, old flows).
--
-- Idempotent; transactional.

BEGIN;

UPDATE edu_ai.prompt_registry
   SET is_active = false
 WHERE prompt_key = 'table_metadata_extraction' AND version = 1;

INSERT INTO edu_ai.prompt_registry
    (prompt_key, version, prompt_text, is_active, component, description,
     placeholders, model_hint, lang, created_by, notes)
SELECT 'table_metadata_extraction', 2,
       p.prompt_text || '

---

STEP 3 — GRADES AND SUBJECTS (PER CHUNK)

Each output element must ALSO contain two more fields:

grades
subjects

"grades": ALL school years (strings "1".."9") the chunk concerns. Resolve
explicit numbers, enumerations and ranges ("grades 1-5" -> "1".."5") and
unambiguous indirect references (Czech "první stupeň" -> 1-5, "druhý
stupeň" -> 6-9, pupil nouns like "deváťáci" -> 9). Empty array when
genuinely undeterminable.

"subjects": subject codes the chunk concerns, chosen ONLY from:
{{SCHOOL_SUBJECTS}}
Empty array when undeterminable. Never output a code outside the list.

The other output rules are unchanged. RETURN JSON ONLY.',
       true, p.component,
       'Table -> row chunks + keywords + grades + subjects in one LLM call (unification, init 117).',
       ARRAY['{tbl_metadata}','{tbl_json}','{{SCHOOL_SUBJECTS}}'],
       p.model_hint, p.lang, 'init-117',
       'v2: grades/subjects per chunk added to the single table call; validated in the insert component.'
FROM edu_ai.prompt_registry p
WHERE p.prompt_key = 'table_metadata_extraction' AND p.version = 1
ON CONFLICT (prompt_key, version) DO NOTHING;

COMMIT;
