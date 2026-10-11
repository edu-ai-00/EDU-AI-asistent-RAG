-- last change in code by Martin Safranek (2026-08-04).
-- 109_school_language_selection.sql
-- A school USES a subset of the platform's languages (decided 2026-08-04).
--
-- Languages stay DEFINED by the platform (the global 'language' rows drive
-- engines, stopwords, system messages — a school cannot invent a language the
-- normalization cannot process). But not every school uses every language:
-- a school's own 'language' rows are its SELECTION. Resolution for 'language'
-- is therefore selection-with-default:
--     school has own language rows -> ITS selection
--     school has none              -> the whole platform set (the default)
-- unlike the school lists (subject/grade), which never fall back (init 108).
--
-- A school's selection may only contain codes the platform defines — trigger.
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
          CASE
              WHEN p_list = 'language' THEN
                  -- selection-with-default: the school's own selection when it
                  -- has one, the platform set otherwise
                  CASE WHEN EXISTS (SELECT 1 FROM edu_ai.code_list x
                                    WHERE x.is_active AND x.list_code = 'language'
                                      AND x.legal_entity_code IN (SELECT legal_entity_code FROM root))
                       THEN (SELECT legal_entity_code FROM root)
                       ELSE NULL END
              -- school lists: the school's own rows or NOTHING (init 108)
              ELSE (SELECT legal_entity_code FROM root)
          END)
$$;

-- a school may only select languages the platform defines
CREATE OR REPLACE FUNCTION edu_ai.language_selection_must_exist()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.list_code = 'language' AND NEW.legal_entity_code IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM edu_ai.code_list
                       WHERE list_code = 'language' AND code = NEW.code
                         AND legal_entity_code IS NULL AND is_active) THEN
        RAISE EXCEPTION 'language %L is not defined by the platform — a school '
                        'selects from the platform languages, it cannot add one',
                        NEW.code;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS language_selection_exists ON edu_ai.code_list;
CREATE TRIGGER language_selection_exists
    BEFORE INSERT OR UPDATE OF list_code, code, legal_entity_code
    ON edu_ai.code_list
    FOR EACH ROW EXECUTE FUNCTION edu_ai.language_selection_must_exist();

COMMIT;
