-- last change in code by Martin Safranek (2026-08-04).
-- 103_grades_subjects_arrays.sql
-- One chunk can speak to SEVERAL grades and subjects — the columns become
-- arrays. A supply-list chunk holds "Třída – 6." and "Třída – 7." side by
-- side, and a doc-level scalar (or a single per-chunk value) can only lie
-- about it. Same for subjects ("český jazyk a matematika").
--
-- Values are číselník codes, filled at ingest from TWO sources unioned:
--   * the uploader's form value (whole document), and
--   * deterministic extraction from the chunk's own text + headings — the
--     same stem patterns the query side uses (prepare.grade_from_text), run
--     over the already-folded norm columns; subjects match the folded labels
--     of code_list('school_subject').
-- Empty array = nothing determined; a PREFERENCE signal only, never a filter.
--
-- The scalar columns from init 102 are dropped, not converted — they were
-- born this morning and the tables were wiped for the ingest test.
--
-- Idempotent; transactional.

BEGIN;

ALTER TABLE edu_ai.document_chunks
    DROP COLUMN IF EXISTS school_grade,
    DROP COLUMN IF EXISTS school_subject;

ALTER TABLE edu_ai.document_chunks
    ADD COLUMN IF NOT EXISTS school_grades   text[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS school_subjects text[] NOT NULL DEFAULT '{}';

COMMENT ON COLUMN edu_ai.document_chunks.school_grades IS
    'code_list(school_grade) codes this chunk speaks to — uploader''s form '
    'value UNION deterministic extraction from the chunk text and headings. '
    'Empty = undetermined. Preference signal only, never a filter.';
COMMENT ON COLUMN edu_ai.document_chunks.school_subjects IS
    'code_list(school_subject) codes — same contract as school_grades.';

-- membership tests during ranking
CREATE INDEX IF NOT EXISTS chunks_school_grades_gin
    ON edu_ai.document_chunks USING gin (school_grades);
CREATE INDEX IF NOT EXISTS chunks_school_subjects_gin
    ON edu_ai.document_chunks USING gin (school_subjects);

COMMIT;
