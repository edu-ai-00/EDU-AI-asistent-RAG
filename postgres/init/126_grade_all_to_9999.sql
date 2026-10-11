-- last change in code by Martin Safranek (2026-08-05).
-- 126_grade_all_to_9999.sql
-- The "all grades" value gets a NUMERIC code 9999 instead of the text 'ALL'
-- (Martin, 2026-08-05): a grade codebook of numbers plus one string mixed
-- types on a column that everything else casts to int, and produktový vlastník's future
-- upper-secondary 10, 11… slot into the same numeric scale. 9999 sits far
-- above any real grade, so an accidental numeric sort puts it last. Chunks
-- reference the row by ID, so their arrays are unaffected — only the code
-- string and the two prompts that name it change.
--
-- (Also here because it belongs to the same decision: the document-level
-- form value is NO LONGER stamped onto every chunk — that is a code change
-- in the insert component, see the flow JSON. Chunk grade/subject columns
-- are pure per-chunk evidence; the document-wide value lives in
-- document_metadata alone.)
--
-- Idempotent; transactional.

BEGIN;

-- 1) the code itself, every owner + the founding template
UPDATE edu_ai.code_list
   SET code = '9999'
 WHERE list_code = 'school_grade' AND code = 'ALL';

-- 2) existing document-level classifications
UPDATE edu_ai.document_metadata
   SET school_grade_user = '9999' WHERE school_grade_user = 'ALL';
UPDATE edu_ai.document_metadata
   SET school_grade_llm = '9999' WHERE school_grade_llm = 'ALL';

-- 3) provisioning keeps 9999 (numeric-only + this sentinel)
CREATE OR REPLACE FUNCTION edu_ai.provision_school_code_lists(p_entity text)
RETURNS integer LANGUAGE plpgsql AS $$
DECLARE n integer;
BEGIN
    INSERT INTO edu_ai.code_list
        (list_code, code, sort_order, is_active, created_by, is_ui_default,
         is_processing_fallback, labels, notes, legal_entity_code)
    SELECT c.list_code, c.code, c.sort_order, c.is_active, 'provisioning',
           c.is_ui_default, false, c.labels,
           'copied from the founding template', p_entity
    FROM edu_ai.code_list c
    WHERE c.legal_entity_code IS NULL
      AND c.list_code IN ('school_subject', 'school_grade')
      AND NOT (c.list_code = 'school_grade'
               AND c.code !~ '^[1-9]$' AND c.code <> '9999')
      AND NOT EXISTS (SELECT 1 FROM edu_ai.code_list x
                      WHERE x.legal_entity_code = p_entity
                        AND x.list_code = c.list_code AND x.code = c.code);
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END $$;

-- 4) document_metadata_extraction v3: name 9999, not ALL
UPDATE edu_ai.prompt_registry SET is_active = false
 WHERE prompt_key = 'document_metadata_extraction' AND version = 2;
INSERT INTO edu_ai.prompt_registry
    (prompt_key, version, prompt_text, is_active, component, description,
     placeholders, model_hint, lang, created_by, notes)
SELECT 'document_metadata_extraction', 3,
       replace(p.prompt_text,
'Use ALL when the document applies to every grade or the school as a whole',
'Use 9999 when the document applies to every grade or the school as a whole'),
       true, p.component, p.description, p.placeholders, p.model_hint, p.lang,
       'init-126', 'v3: all-grades code ALL -> 9999.'
FROM edu_ai.prompt_registry p
WHERE p.prompt_key = 'document_metadata_extraction' AND p.version = 2
ON CONFLICT (prompt_key, version) DO NOTHING;

-- 5) search_answer_generation v7: 9999 means every grade
UPDATE edu_ai.prompt_registry SET is_active = false
 WHERE prompt_key = 'search_answer_generation' AND version = 6;
INSERT INTO edu_ai.prompt_registry
    (prompt_key, version, prompt_text, is_active, component, description,
     placeholders, model_hint, lang, created_by, notes)
SELECT 'search_answer_generation', 7,
       replace(p.prompt_text,
'"ALL" means every grade',
'"9999" means every grade'),
       true, p.component, p.description, p.placeholders, p.model_hint, p.lang,
       'init-126', 'v7: all-grades code ALL -> 9999.'
FROM edu_ai.prompt_registry p
WHERE p.prompt_key = 'search_answer_generation' AND p.version = 6
ON CONFLICT (prompt_key, version) DO NOTHING;

COMMIT;
