-- last change in code by Martin Safranek (2026-08-04).
-- 110_numeric_grades_only.sql
-- A school's grade číselník holds NUMBERS ONLY (decided 2026-08-04): the
-- UNKNOWN row makes no sense as a chunk reference — "undetermined" is the
-- EMPTY array, and offering UNKNOWN in a form select invites storing a
-- non-answer. UNKNOWN stays in the founding TEMPLATE only, where the
-- document-level metadata extraction ({{SCHOOL_GRADES}} substitution and the
-- school_grade_llm column) still needs a name for "cannot tell".
--
-- Idempotent; transactional.

BEGIN;

-- schools: numeric only
UPDATE edu_ai.code_list
   SET is_active = false
 WHERE list_code = 'school_grade'
   AND legal_entity_code IS NOT NULL
   AND code !~ '^[1-9]$'
   AND is_active;

-- provisioning copies numeric grades only from now on
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
      AND NOT (c.list_code = 'school_grade' AND c.code !~ '^[1-9]$')
      AND NOT EXISTS (SELECT 1 FROM edu_ai.code_list x
                      WHERE x.legal_entity_code = p_entity
                        AND x.list_code = c.list_code AND x.code = c.code);
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END $$;

COMMIT;
