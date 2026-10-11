-- last change in code by Martin Safranek (2026-08-04).
-- 102_grade_subject_on_chunks.sql
-- Grade becomes a NUMBER, and grade + subject move onto the chunks.
--
-- ── why plain numbers (decided 2026-08-04) ──────────────────────────────────
-- The ZS6/SS1 codes existed because a bare "1" was ambiguous — first year of
-- primary or of secondary? Since the multi-entity register, the UNIT carries
-- that information (legal_entity.level = ZS/SS), so "grade 6 + the ZŠ unit" is
-- unambiguous on its own and the prefix codes lose their reason to exist.
-- code_list('school_grade') becomes '1'..'9' (+ UNKNOWN); a secondary school
-- uses 1..4 of the same set. The one caller this cannot disambiguate — a
-- top-level entity with no level — is harmless, because the grade only ever
-- PREFERS content, it never excludes.
--
-- ── why chunk-level columns, and why NO norm/lemma/stem for them ────────────
-- Grade and subject are CATEGORIES, not prose: číselník codes compared by
-- equality, so there is nothing to lemmatize — the reduced-form columns exist
-- for fulltext over inflected words. Mapping a human query ("matika pro
-- šesťáky") onto the codes happens on the QUERY side (labels + synonyms +
-- deterministic grade extraction), never by normalising a code column.
-- On the chunks (not only document metadata) for the same reason as
-- legal_entity_code and visibility: the retrieval steps rank DURING the scan,
-- and a per-chunk value also allows a later per-SECTION refinement (a supply
-- list holds grades 1..9 in one document — the document-level value cannot
-- say which section belongs to whom).
--
-- Idempotent; transactional.

BEGIN;

-- ── the grade číselník speaks numbers ───────────────────────────────────────
UPDATE edu_ai.code_list SET is_active = false
 WHERE list_code = 'school_grade' AND code ~ '^(ZS|SS|MS)\d$' AND is_active;

INSERT INTO edu_ai.code_list
    (list_code, code, sort_order, is_active, created_by, is_ui_default,
     is_processing_fallback, labels)
SELECT 'school_grade', g::text, g * 10, true, 'init-102', false, false,
       jsonb_build_object(
           'cs', g || '. ročník',
           'en', 'Grade ' || g,
           'de', g || '. Klasse',
           'es', g || '.º curso')
FROM generate_series(1, 9) g
WHERE NOT EXISTS (SELECT 1 FROM edu_ai.code_list
                  WHERE list_code = 'school_grade' AND code = g::text);

-- ── grade + subject on every chunk (číselník codes; NULL = undetermined) ────
ALTER TABLE edu_ai.document_chunks
    ADD COLUMN IF NOT EXISTS school_grade   text,
    ADD COLUMN IF NOT EXISTS school_subject text;

COMMENT ON COLUMN edu_ai.document_chunks.school_grade IS
    'code_list(school_grade) code — a plain number 1..9 (SŠ uses 1..4; the '
    'unit''s level disambiguates). Inherited from the document at ingest, may '
    'be refined per section later. Preference signal only, never a filter.';
COMMENT ON COLUMN edu_ai.document_chunks.school_subject IS
    'code_list(school_subject) code. Same contract as school_grade.';

COMMIT;
