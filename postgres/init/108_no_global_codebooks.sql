-- last change in code by Martin Safranek (2026-08-04).
-- 108_no_global_codebooks.sql
-- Číselníky are the SCHOOLS' only (decided 2026-08-04, superseding the
-- global-fallback of init 107): at runtime a school's subject and grade lists
-- are its own rows, full stop. Rows with legal_entity_code IS NULL are not a
-- runtime fallback any more — they are the TEMPLATE copied when a new school
-- is founded (edu_ai.provision_school_code_lists), and nothing else reads
-- them. A school whose lists were not provisioned resolves to an EMPTY set:
-- detection finds nothing, validation accepts nothing — loud and visible,
-- never a silent fallback to somebody else's list.
--
-- BOUNDARY: code_list('language') stays a PLATFORM registry. Languages drive
-- the normalization engines, stopwords and system messages across the whole
-- system; a per-school language list would mean per-school normalization.
-- resolved_code_list therefore serves school-only sets for the SCHOOL lists
-- and the global set for 'language'.
--
-- Idempotent; transactional.

BEGIN;

CREATE OR REPLACE FUNCTION edu_ai.resolved_code_list(p_entity text, p_list text)
RETURNS SETOF edu_ai.code_list LANGUAGE sql STABLE AS $$
    WITH RECURSIVE up AS (
        SELECT legal_entity_code, parent_code
        FROM edu_ai.legal_entity WHERE legal_entity_code = p_entity
        UNION ALL
        SELECT l.legal_entity_code, l.parent_code
        FROM edu_ai.legal_entity l JOIN up ON l.legal_entity_code = up.parent_code
    ), root AS (
        SELECT legal_entity_code FROM up WHERE parent_code IS NULL
    )
    SELECT c.* FROM edu_ai.code_list c
    WHERE c.is_active AND c.list_code = p_list
      AND c.legal_entity_code IS NOT DISTINCT FROM (
          CASE WHEN p_list = 'language'
               THEN NULL                                   -- platform registry
               ELSE (SELECT legal_entity_code FROM root)   -- the school's own, or nothing
          END)
$$;

COMMENT ON FUNCTION edu_ai.resolved_code_list(text, text) IS
    'The entity''s číselník set: the organisation''s OWN rows for school lists '
    '(no global fallback — empty when not provisioned), the global rows for '
    '''language'' (platform registry). NULL-owner school rows are only the '
    'founding template, see provision_school_code_lists.';

-- founding a school = copying the template
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
      AND NOT EXISTS (SELECT 1 FROM edu_ai.code_list x
                      WHERE x.legal_entity_code = p_entity
                        AND x.list_code = c.list_code AND x.code = c.code);
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END $$;

COMMIT;
