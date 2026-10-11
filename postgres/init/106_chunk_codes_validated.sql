-- last change in code by Martin Safranek (2026-08-04).
-- 106_chunk_codes_validated.sql
-- The chunk's grade/subject arrays are validated AGAINST THE ČÍSELNÍK in the
-- database, not only in code.
--
-- Postgres cannot put a foreign key on array elements, so the reference
-- integrity a FK would give lives in a trigger: every element of
-- school_grades must be an active code_list('school_grade') code, every
-- element of school_subjects an active code_list('school_subject') code.
-- The pipeline already selects only codebook values (deterministic pass picks
-- from the číselník, the LLM output is filtered against it) — this is the
-- backstop that turns a future bug into a loud error instead of silent junk.
--
-- Codes, not numeric ids, by decision (2026-08-04): code_list.id is GENERATED
-- ALWAYS, so a clean install renumbers it — ids would not survive across
-- environments, codes do. Same identity rule as everywhere else in the system
-- (languages, document types, grades). "Undetermined" stays the EMPTY array,
-- not an UNKNOWN element — the fusion treats {} as neutral.
--
-- Idempotent; transactional.

BEGIN;

CREATE OR REPLACE FUNCTION edu_ai.chunk_codes_valid()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE bad text;
BEGIN
    SELECT g INTO bad FROM unnest(NEW.school_grades) AS g
     WHERE NOT EXISTS (SELECT 1 FROM edu_ai.code_list
                       WHERE list_code = 'school_grade'
                         AND code = g AND is_active)
     LIMIT 1;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'school_grades element %L is not an active code_list(school_grade) code', bad;
    END IF;
    SELECT s INTO bad FROM unnest(NEW.school_subjects) AS s
     WHERE NOT EXISTS (SELECT 1 FROM edu_ai.code_list
                       WHERE list_code = 'school_subject'
                         AND code = s AND is_active)
     LIMIT 1;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'school_subjects element %L is not an active code_list(school_subject) code', bad;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS chunk_codes_valid ON edu_ai.document_chunks;
CREATE TRIGGER chunk_codes_valid
    BEFORE INSERT OR UPDATE OF school_grades, school_subjects
    ON edu_ai.document_chunks
    FOR EACH ROW EXECUTE FUNCTION edu_ai.chunk_codes_valid();

COMMIT;
