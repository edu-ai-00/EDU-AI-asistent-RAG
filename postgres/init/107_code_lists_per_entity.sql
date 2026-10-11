-- last change in code by Martin Safranek (2026-08-04).
-- 107_code_lists_per_entity.sql
-- Číselníky per organizace + chunk references by ROW ID (decided 2026-08-04).
--
-- ── code_list / document_type_taxonomy get an owner ─────────────────────────
-- legal_entity_code NULL = the global set; a row may be owned by a TOP-LEVEL
-- organisation only (same trigger rule as settings). RESOLUTION PER LIST is a
-- set REPLACEMENT, not a merge ("každá entita má svoji množinu záznamů"): when
-- an organisation has ANY active rows for a list_code, only ITS rows apply;
-- otherwise the global rows do.
--
-- ── chunks reference the school's ROW ID ────────────────────────────────────
-- school_grade_ids / school_subject_ids (bigint[]) replace the code arrays:
-- the requirement is to SEE which row of WHICH school's číselník the chunk
-- speaks to, and with per-school sets a bare code cannot say that. The earlier
-- id-vs-code concern (a clean install renumbers ids) does not bite here:
-- chunks are CONTENT, never seeded — they are re-ingested per installation,
-- so their ids and the codebook ids are born together. The trigger validates
-- every element: an active row, the right list, and owned by the chunk's own
-- organisation or global (per the resolution above).
--
-- ── seeding existing schools ────────────────────────────────────────────────
-- Each existing top-level school receives its own copy of school_subject and
-- school_grade (the two lists that genuinely differ between schools).
-- Languages and document types stay global until a school needs its own.
--
-- Idempotent; transactional.

BEGIN;

ALTER TABLE edu_ai.code_list
    ADD COLUMN IF NOT EXISTS legal_entity_code text
        REFERENCES edu_ai.legal_entity (legal_entity_code);
ALTER TABLE edu_ai.document_type_taxonomy
    ADD COLUMN IF NOT EXISTS legal_entity_code text
        REFERENCES edu_ai.legal_entity (legal_entity_code);

-- owner must be a top-level organisation (same rule as settings, init 98)
CREATE OR REPLACE FUNCTION edu_ai.codebook_owner_must_be_root()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.legal_entity_code IS NOT NULL AND EXISTS (
        SELECT 1 FROM edu_ai.legal_entity
        WHERE legal_entity_code = NEW.legal_entity_code
          AND parent_code IS NOT NULL) THEN
        RAISE EXCEPTION 'codebook rows belong to a TOP-LEVEL organisation '
                        '(% has a parent) or to nobody (global)',
                        NEW.legal_entity_code;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS code_list_owner_root ON edu_ai.code_list;
CREATE TRIGGER code_list_owner_root
    BEFORE INSERT OR UPDATE OF legal_entity_code ON edu_ai.code_list
    FOR EACH ROW EXECUTE FUNCTION edu_ai.codebook_owner_must_be_root();
DROP TRIGGER IF EXISTS taxonomy_owner_root ON edu_ai.document_type_taxonomy;
CREATE TRIGGER taxonomy_owner_root
    BEFORE INSERT OR UPDATE OF legal_entity_code ON edu_ai.document_type_taxonomy
    FOR EACH ROW EXECUTE FUNCTION edu_ai.codebook_owner_must_be_root();

-- uniqueness now includes the owner
DROP INDEX IF EXISTS edu_ai.code_list_active_code;
CREATE UNIQUE INDEX IF NOT EXISTS code_list_active_code
    ON edu_ai.code_list (legal_entity_code, list_code, code)
    NULLS NOT DISTINCT WHERE is_active;
DROP INDEX IF EXISTS edu_ai.code_list_one_processing_fallback;
CREATE UNIQUE INDEX IF NOT EXISTS code_list_one_processing_fallback
    ON edu_ai.code_list (legal_entity_code, list_code)
    NULLS NOT DISTINCT WHERE (is_processing_fallback AND is_active);
DROP INDEX IF EXISTS edu_ai.code_list_one_ui_default;
CREATE UNIQUE INDEX IF NOT EXISTS code_list_one_ui_default
    ON edu_ai.code_list (legal_entity_code, list_code)
    NULLS NOT DISTINCT WHERE (is_ui_default AND is_active);
DROP INDEX IF EXISTS edu_ai.document_type_taxonomy_active_code;
CREATE UNIQUE INDEX IF NOT EXISTS document_type_taxonomy_active_code
    ON edu_ai.document_type_taxonomy (legal_entity_code, type_code)
    NULLS NOT DISTINCT WHERE is_active;

-- ── the resolved set of one list for one entity, as a FUNCTION ──────────────
-- One definition used by the validation trigger and free to use in queries:
-- the organisation's own rows when it has any for the list, else global.
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
          CASE WHEN EXISTS (SELECT 1 FROM edu_ai.code_list x
                            WHERE x.is_active AND x.list_code = p_list
                              AND x.legal_entity_code IN (SELECT legal_entity_code FROM root))
               THEN (SELECT legal_entity_code FROM root)
               ELSE NULL END)
$$;

-- ── chunks: references by ID of the school's row ────────────────────────────
DROP TRIGGER IF EXISTS chunk_codes_valid ON edu_ai.document_chunks;
ALTER TABLE edu_ai.document_chunks
    DROP COLUMN IF EXISTS school_grades,
    DROP COLUMN IF EXISTS school_subjects;
ALTER TABLE edu_ai.document_chunks
    ADD COLUMN IF NOT EXISTS school_grade_ids   bigint[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS school_subject_ids bigint[] NOT NULL DEFAULT '{}';
COMMENT ON COLUMN edu_ai.document_chunks.school_grade_ids IS
    'code_list row IDS (school''s own resolved set) the chunk speaks to. '
    'Empty = undetermined. Preference signal only, never a filter. Init 107.';
COMMENT ON COLUMN edu_ai.document_chunks.school_subject_ids IS
    'Same contract as school_grade_ids, list_code = school_subject.';
CREATE INDEX IF NOT EXISTS chunks_school_grade_ids_gin
    ON edu_ai.document_chunks USING gin (school_grade_ids);
CREATE INDEX IF NOT EXISTS chunks_school_subject_ids_gin
    ON edu_ai.document_chunks USING gin (school_subject_ids);

DROP TRIGGER IF EXISTS chunk_codes_valid ON edu_ai.document_chunks;
CREATE OR REPLACE FUNCTION edu_ai.chunk_codes_valid()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE bad bigint;
BEGIN
    SELECT g INTO bad FROM unnest(NEW.school_grade_ids) AS g
     WHERE NOT EXISTS (SELECT 1 FROM edu_ai.resolved_code_list(
                           NEW.legal_entity_code, 'school_grade') r
                       WHERE r.id = g)
     LIMIT 1;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'school_grade_ids: % is not a row of the resolved school_grade list for %',
                        bad, NEW.legal_entity_code;
    END IF;
    SELECT s INTO bad FROM unnest(NEW.school_subject_ids) AS s
     WHERE NOT EXISTS (SELECT 1 FROM edu_ai.resolved_code_list(
                           NEW.legal_entity_code, 'school_subject') r
                       WHERE r.id = s)
     LIMIT 1;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'school_subject_ids: % is not a row of the resolved school_subject list for %',
                        bad, NEW.legal_entity_code;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER chunk_codes_valid
    BEFORE INSERT OR UPDATE OF school_grade_ids, school_subject_ids
    ON edu_ai.document_chunks
    FOR EACH ROW EXECUTE FUNCTION edu_ai.chunk_codes_valid();

-- ── existing schools get their own subject + grade sets ─────────────────────
INSERT INTO edu_ai.code_list
    (list_code, code, sort_order, is_active, created_by, is_ui_default,
     is_processing_fallback, labels, notes, legal_entity_code)
SELECT c.list_code, c.code, c.sort_order, c.is_active, 'init-107',
       c.is_ui_default, false, c.labels, 'per-school copy of the global set',
       e.legal_entity_code
FROM edu_ai.code_list c
CROSS JOIN edu_ai.legal_entity e
WHERE c.legal_entity_code IS NULL
  AND c.list_code IN ('school_subject', 'school_grade')
  AND e.parent_code IS NULL AND e.is_active
  AND NOT EXISTS (SELECT 1 FROM edu_ai.code_list x
                  WHERE x.legal_entity_code = e.legal_entity_code
                    AND x.list_code = c.list_code AND x.code = c.code);

COMMIT;
