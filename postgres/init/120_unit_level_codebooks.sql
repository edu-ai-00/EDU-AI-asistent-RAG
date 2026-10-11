-- last change in code by Martin Safranek (2026-08-05).
-- 120_unit_level_codebooks.sql
-- Codebooks may now be owned by a UNIT, not only the organisation (decided
-- 2026-08-05): a mixed organisation (Nová Ves = ZŠ + SOŠ under one legal
-- person) has ONE grade list today, so the secondary school "has" grades
-- 1..9 and its "1. ročník" would collide with the primary school's 1st
-- grade in the same corpus.
--
-- Resolution becomes NEAREST OWNER UP THE TREE (self first, then parents):
--   - a unit with its own rows uses them (SOŠ -> 1..4),
--   - a unit without rows inherits the organisation's (ZŠ -> the org's 1..9),
--   - school lists still never fall back to the founding template,
--   - 'language' keeps selection-with-default: the nearest selection up the
--     tree, else the platform set.
-- Every consumer goes through resolved_code_list, so ingest, validation,
-- search and the UI pick this up with no code change.
--
-- Idempotent; transactional.

BEGIN;

-- 1) codebook rows may be owned by any REGISTERED entity (was: root only)
CREATE OR REPLACE FUNCTION edu_ai.codebook_owner_must_be_root()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    -- The name is historical (init 107 allowed top-level owners only). Since
    -- init 120 a codebook row may belong to ANY registered entity — a unit
    -- owns a list exactly when its semantics differ from its organisation's
    -- (SOŠ grades 1..4). NULL stays the founding template.
    IF NEW.legal_entity_code IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM edu_ai.legal_entity
        WHERE legal_entity_code = NEW.legal_entity_code) THEN
        RAISE EXCEPTION 'codebook owner % is not in the register',
                        NEW.legal_entity_code;
    END IF;
    RETURN NEW;
END $$;

-- 2) nearest-owner-up resolution
CREATE OR REPLACE FUNCTION edu_ai.resolved_code_list(p_entity text, p_list text)
RETURNS SETOF edu_ai.code_list LANGUAGE sql STABLE AS $$
    WITH RECURSIVE up AS (
        SELECT legal_entity_code, parent_code, 0 AS depth
        FROM edu_ai.legal_entity WHERE legal_entity_code = p_entity
        UNION ALL
        SELECT l.legal_entity_code, l.parent_code, up.depth + 1
        FROM edu_ai.legal_entity l JOIN up ON l.legal_entity_code = up.parent_code
    ), owner AS (
        -- the nearest node up the tree (self first) that OWNS rows of p_list
        SELECT u.legal_entity_code
        FROM up u
        WHERE EXISTS (SELECT 1 FROM edu_ai.code_list c
                      WHERE c.is_active AND c.list_code = p_list
                        AND c.legal_entity_code = u.legal_entity_code)
        ORDER BY u.depth
        LIMIT 1
    )
    SELECT c.* FROM edu_ai.code_list c
    WHERE c.is_active AND c.list_code = p_list
      AND c.legal_entity_code IS NOT DISTINCT FROM (
          CASE WHEN p_list = 'language' THEN
                   -- selection-with-default: nearest selection, else platform
                   (SELECT legal_entity_code FROM owner)
               ELSE
                   -- school lists: nearest owner or NOTHING — the sentinel
                   -- keeps a missing owner from matching the NULL template
                   COALESCE((SELECT legal_entity_code FROM owner), '(nobody)')
          END)
$$;

COMMENT ON FUNCTION edu_ai.resolved_code_list(text, text) IS
    'The entity''s číselník set: the NEAREST owner up the tree (self, then '
    'parents) for school lists — a unit may own a list when its semantics '
    'differ (SOŠ grades 1..4), otherwise it inherits its organisation''s; '
    'never the NULL template. ''language'' is the nearest selection up the '
    'tree, else the platform set. Init 120.';

-- 3) secondary-school units get their own grade list 1..4 (labels copied
--    from the founding template rows 1..4 — "1. ročník" reads fine, the unit
--    is the context)
INSERT INTO edu_ai.code_list
    (list_code, code, sort_order, is_active, created_by, is_ui_default,
     is_processing_fallback, labels, notes, legal_entity_code)
SELECT 'school_grade', t.code, t.sort_order, true, 'init-120',
       t.is_ui_default, false, t.labels,
       'SŠ ročníky jednotky — per-unit číselník (init 120)',
       u.legal_entity_code
FROM edu_ai.legal_entity u
JOIN edu_ai.code_list t
  ON t.legal_entity_code IS NULL AND t.list_code = 'school_grade'
 AND t.is_active AND t.code IN ('1', '2', '3', '4')
WHERE u.level = 'SS' AND u.parent_code IS NOT NULL AND u.is_active
  AND NOT EXISTS (SELECT 1 FROM edu_ai.code_list x
                  WHERE x.legal_entity_code = u.legal_entity_code
                    AND x.list_code = 'school_grade' AND x.code = t.code);

COMMIT;
