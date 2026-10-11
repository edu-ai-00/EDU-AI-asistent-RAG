-- last change in code by Martin Safranek (2026-07-29).
-- 40_jsonb_labels_and_pack_guard.sql
-- 1) LABELS AS DATA. document_type_taxonomy and code_list carried the display
--    names in FIXED columns label_cs + label_en, so a third language meant DDL
--    plus code changes in the two endpoints and both test pages — the one place
--    that broke the pattern used everywhere else (qa_stopword, qa_synonym,
--    qa_blocklist, system_message all carry a `lang` column, i.e. language is
--    DATA). A single jsonb column {"cs": …, "en": …} makes adding a language an
--    UPDATE: `SET labels = labels || '{"de": "Studienmaterial"}'`.
--    The old columns are KEPT and still filled (the endpoints return both), so
--    nothing that reads label_cs/label_en breaks; they can be dropped in a
--    later migration once every consumer reads `labels`.
-- 2) PACK GUARD. "exactly one active version per pack_id" was enforced only by
--    the insert component's UPDATE-then-INSERT sequence. A crash between the
--    two steps — or a manual `SET is_active = true` (done twice while restoring
--    cs-core on 2026-07-28) — silently produced two active rows, and
--    load_thresholds_by_pack would then pick one of them nondeterministically.
--    The partial unique index makes that state impossible, matching the pattern
--    already used on search_setting and the číselníky.
-- Idempotent; transactional.

BEGIN;

ALTER TABLE edu_ai.document_type_taxonomy ADD COLUMN IF NOT EXISTS labels jsonb;
ALTER TABLE edu_ai.code_list              ADD COLUMN IF NOT EXISTS labels jsonb;

UPDATE edu_ai.document_type_taxonomy
SET labels = jsonb_build_object('cs', label_cs, 'en', label_en)
WHERE labels IS NULL;

UPDATE edu_ai.code_list
SET labels = jsonb_build_object('cs', label_cs, 'en', label_en)
WHERE labels IS NULL;

ALTER TABLE edu_ai.document_type_taxonomy ALTER COLUMN labels SET NOT NULL;
ALTER TABLE edu_ai.code_list              ALTER COLUMN labels SET NOT NULL;

-- one active version per pack — schema-level guarantee, not just convention
CREATE UNIQUE INDEX IF NOT EXISTS qa_pack_manifest_one_active_per_pack
    ON edu_ai.qa_pack_manifest (pack_id) WHERE is_active;

COMMIT;
