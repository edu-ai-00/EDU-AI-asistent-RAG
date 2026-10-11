-- last change in code by Martin Safranek (2026-08-05).
-- 121_grade_all_value.sql
-- New grade value "Pro všechny ročníky" (code ALL) — produktový vlastník, 2026-08-05.
-- A document like a school code applies to every grade; forcing the uploader
-- to enumerate 1..9 (or leave it empty = "undetermined") lied either way.
--
-- Nová Ves gets produktový vlastník's exact shape on top of the unit-level codebooks
-- (init 120): the ZŠ unit owns 1..9 + ALL, the SOŠ unit 1..4 + ALL, and the
-- TOP entity keeps ALL as its ONLY active value — organisation-wide
-- documents are by definition for everyone. Single-entity schools (Borová,
-- Lipová) keep 1..9 and gain ALL alongside.
--
-- The chunks of the one existing ZŠ-unit document are remapped from the
-- organisation's row ids to the unit's new own rows (same codes, new owner) —
-- the validation trigger and the fusion bonus both key on the RESOLVED set,
-- which for the unit now serves its own rows.
--
-- Idempotent; transactional.

BEGIN;

-- 1) founding template: ALL (active — new schools get it via provisioning)
INSERT INTO edu_ai.code_list
    (list_code, code, sort_order, is_active, created_by, is_ui_default,
     is_processing_fallback, labels, notes, legal_entity_code)
SELECT 'school_grade', 'ALL', 0, true, 'init-121', false, false,
       '{"cs": "Pro všechny ročníky", "en": "All grades",
         "de": "Alle Jahrgänge", "es": "Todos los cursos"}'::jsonb,
       'dokument platí pro všechny ročníky', NULL
WHERE NOT EXISTS (SELECT 1 FROM edu_ai.code_list
                  WHERE list_code = 'school_grade' AND code = 'ALL'
                    AND legal_entity_code IS NULL);

-- 2) provisioning copies ALL along with the numeric grades from now on
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
               AND c.code !~ '^[1-9]$' AND c.code <> 'ALL')
      AND NOT EXISTS (SELECT 1 FROM edu_ai.code_list x
                      WHERE x.legal_entity_code = p_entity
                        AND x.list_code = c.list_code AND x.code = c.code);
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END $$;

-- 3) Nová Ves ZŠ unit takes over the organisation's numeric grades as its
--    OWN rows (the org will keep only ALL)
INSERT INTO edu_ai.code_list
    (list_code, code, sort_order, is_active, created_by, is_ui_default,
     is_processing_fallback, labels, notes, legal_entity_code)
SELECT 'school_grade', o.code, o.sort_order, true, 'init-121',
       o.is_ui_default, false, o.labels,
       'ZŠ ročníky jednotky — per-unit číselník (init 121)',
       'SKOLA_NOVA_VES_ZS'
FROM edu_ai.code_list o
WHERE o.legal_entity_code = 'SKOLA_NOVA_VES'
  AND o.list_code = 'school_grade' AND o.is_active AND o.code ~ '^[1-9]$'
  AND NOT EXISTS (SELECT 1 FROM edu_ai.code_list x
                  WHERE x.legal_entity_code = 'SKOLA_NOVA_VES_ZS'
                    AND x.list_code = 'school_grade' AND x.code = o.code);

-- 4) every entity that OWNS an active grade list gets ALL alongside
--    (Lipová, Borová, Nová Ves org, Nová Ves ZŠ, Nová Ves SOŠ)
INSERT INTO edu_ai.code_list
    (list_code, code, sort_order, is_active, created_by, is_ui_default,
     is_processing_fallback, labels, notes, legal_entity_code)
SELECT DISTINCT 'school_grade', 'ALL', 0, true, 'init-121', false, false,
       '{"cs": "Pro všechny ročníky", "en": "All grades",
         "de": "Alle Jahrgänge", "es": "Todos los cursos"}'::jsonb,
       'dokument platí pro všechny ročníky', c.legal_entity_code
FROM edu_ai.code_list c
WHERE c.list_code = 'school_grade' AND c.is_active
  AND c.legal_entity_code IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM edu_ai.code_list x
                  WHERE x.legal_entity_code = c.legal_entity_code
                    AND x.list_code = 'school_grade' AND x.code = 'ALL');

-- 5) the ZŠ-unit document's chunks: organisation row ids -> the unit's new
--    own ids (same code, new owner) — BEFORE the org rows are deactivated
UPDATE edu_ai.document_chunks c
SET school_grade_ids = (
    SELECT coalesce(array_agg(DISTINCT coalesce(z.id, oid)), '{}')
    FROM unnest(c.school_grade_ids) AS oid
    LEFT JOIN edu_ai.code_list o ON o.id = oid
    LEFT JOIN edu_ai.code_list z
           ON z.legal_entity_code = 'SKOLA_NOVA_VES_ZS'
          AND z.list_code = 'school_grade' AND z.code = o.code)
FROM edu_ai.document_metadata m
WHERE m.file_uuid = c.file_uuid
  AND m.legal_entity_code = 'SKOLA_NOVA_VES_ZS'
  AND c.school_grade_ids <> '{}';

-- 6) the TOP entity keeps ALL as its only active grade value
UPDATE edu_ai.code_list
   SET is_active = false
 WHERE legal_entity_code = 'SKOLA_NOVA_VES'
   AND list_code = 'school_grade' AND code ~ '^[1-9]$' AND is_active;

COMMIT;
