-- last change in code by Martin Safranek (2026-07-29).
-- 42_ui_default_vs_processing_fallback.sql
-- Two flags on the číselník that LOOK alike and mean entirely different things.
-- Naming them apart is the point of this migration, not a cosmetic rename:
--
--   is_ui_default          which option a FORM preselects. Presentation only.
--                          Nothing in the ingest or search path may read it.
--   is_processing_fallback  which language the PIPELINE uses when the language
--                          cannot be determined from the content at all.
--
-- Why they must not be one flag: Czech being the sensible preselection in a
-- Czech school's upload form says nothing about what an undecidable English
-- table fragment should be reduced with. Conflating them is how a hidden Czech
-- bias gets into the index — the exact class of bug found on 2026-07-29, where
-- a bilingual document had 14 chunks stemmed in the wrong language.
--
-- The fallback lives HERE, on the registry row, rather than as a string in
-- search_setting.config, so that it is STRUCTURALLY a valid active language:
-- a JSON string could name a language that does not exist or was deactivated,
-- and that failure would be silent. code_list('language') is the single
-- registry — adding a language starts here, and every consumer (search API,
-- web chat, ingestion API) reads the available languages from it.
--
-- Partial indexes are scoped to `is_active` (unlike the old one_default index,
-- which was not): a DEACTIVATED row holding the flag must not occupy the single
-- slot and block a new active default/fallback. This matches every other
-- partial index in the schema (code_list_active_code,
-- qa_pack_manifest_one_active_per_pack).
--
-- Idempotent; transactional. No data is reduced or re-indexed by this migration
-- — the seeded fallback ('cs') is what the code guessed implicitly until now,
-- so stored tokens stay valid and no re-ingest is needed.

BEGIN;

-- 1) rename: presentation flag says so in its name -------------------------
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'edu_ai' AND table_name = 'code_list'
                 AND column_name = 'is_default') THEN
        ALTER TABLE edu_ai.code_list RENAME COLUMN is_default TO is_ui_default;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'edu_ai' AND table_name = 'document_type_taxonomy'
                 AND column_name = 'is_default') THEN
        ALTER TABLE edu_ai.document_type_taxonomy
            RENAME COLUMN is_default TO is_ui_default;
    END IF;
END $$;

-- 2) the processing fallback -----------------------------------------------
-- Generic column on the generic číselník table, but only the 'language' list
-- has a consumer today. Other lists may adopt it (a fallback school subject is
-- conceivable); until then their rows simply stay false.
ALTER TABLE edu_ai.code_list
    ADD COLUMN IF NOT EXISTS is_processing_fallback boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN edu_ai.code_list.is_ui_default IS
    'Presentation only: which option a form preselects. MUST NOT be read by the '
    'ingest or search path — see is_processing_fallback for that.';
COMMENT ON COLUMN edu_ai.code_list.is_processing_fallback IS
    'Pipeline fallback: used when the value cannot be determined from content '
    '(language detection returned nothing). Never a UI concern.';
COMMENT ON COLUMN edu_ai.document_type_taxonomy.is_ui_default IS
    'Presentation only: which option a form preselects.';

-- 3) guards — one per list, among ACTIVE rows only -------------------------
DROP INDEX IF EXISTS edu_ai.code_list_one_default;
DROP INDEX IF EXISTS edu_ai.document_type_taxonomy_one_default;

CREATE UNIQUE INDEX IF NOT EXISTS code_list_one_ui_default
    ON edu_ai.code_list (list_code) WHERE is_ui_default AND is_active;

CREATE UNIQUE INDEX IF NOT EXISTS code_list_one_processing_fallback
    ON edu_ai.code_list (list_code) WHERE is_processing_fallback AND is_active;

CREATE UNIQUE INDEX IF NOT EXISTS document_type_taxonomy_one_ui_default
    ON edu_ai.document_type_taxonomy ((true)) WHERE is_ui_default AND is_active;

-- 4) seed: Czech is today's implicit fallback, now stated explicitly -------
-- This is the value detect_lang used to return as its hardcoded `default`.
-- Making it data means a German-only deployment changes one row instead of
-- code, and the readiness check can assert that every active language layer
-- exists.
UPDATE edu_ai.code_list
SET is_processing_fallback = true
WHERE list_code = 'language' AND code = 'cs' AND is_active
  AND NOT EXISTS (
      SELECT 1 FROM edu_ai.code_list
      WHERE list_code = 'language' AND is_processing_fallback AND is_active);

COMMIT;
