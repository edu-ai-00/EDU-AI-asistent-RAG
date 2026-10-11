-- last change in code by Martin Safranek (2026-07-29).
-- 41_drop_legacy_label_columns.sql
-- Finish what migration 40 started: labels are DATA, not schema.
--
-- Migration 40 added `labels jsonb` ({"cs": …, "en": …}) to both číselník tables
-- and backfilled it from label_cs/label_en, but KEPT the old columns so nothing
-- reading them would break. Every consumer has since been migrated:
--   * BatchRunComponent-r6dJ7-MD (MARKDOWN flow, document classification) now
--     reads labels->>'en' — it needs the ENGLISH name because the classification
--     prompt is English, which keeps classification independent of the document's
--     language. This was the last functional dependency.
--   * Search API /document-types and /code-lists no longer select or return them.
--   * tools/upload_test.html + tools/search_test.html read `labels` (the
--     label_cs/label_en fallback in pickLabel was removed with this migration).
-- Keeping two representations of the same fact invites drift: a language added by
-- UPDATE-ing `labels` would silently not appear in whatever still read the fixed
-- columns. Dropping them makes `labels` the single source of truth, so adding a
-- language stays a pure data change (see docs/ADDING_A_LANGUAGE.md).
--
-- Rollback: re-add the columns and backfill from labels —
--   ALTER TABLE … ADD COLUMN label_cs text; UPDATE … SET label_cs = labels->>'cs';
-- Idempotent; transactional.

BEGIN;

ALTER TABLE edu_ai.document_type_taxonomy
    DROP COLUMN IF EXISTS label_cs,
    DROP COLUMN IF EXISTS label_en;

ALTER TABLE edu_ai.code_list
    DROP COLUMN IF EXISTS label_cs,
    DROP COLUMN IF EXISTS label_en;

COMMIT;
