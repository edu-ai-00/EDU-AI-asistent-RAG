-- last change in code by Martin Safranek (2026-07-27).
-- 32_code_list_defaults.sql
-- Default values for the číselníky (agreed 2026-07-27): a data-driven
-- `is_default` flag on code_list and document_type_taxonomy. The select-menu
-- UI PRE-SELECTS the default; the gateway does NOT silently inject it — the
-- *_user fields keep their "user did not choose" semantics (NULL), which the
-- two-field pattern needs for missing/match/contradiction derivation.
-- Seeded default: language = 'cs' (Czech school platform). Type/subject/grade
-- deliberately have NO default — the LLM classifies them better than a static
-- guess. At most one default per list (partial unique index). Idempotent.

ALTER TABLE edu_ai.code_list
    ADD COLUMN IF NOT EXISTS is_default boolean NOT NULL DEFAULT false;
ALTER TABLE edu_ai.document_type_taxonomy
    ADD COLUMN IF NOT EXISTS is_default boolean NOT NULL DEFAULT false;

CREATE UNIQUE INDEX IF NOT EXISTS code_list_one_default
    ON edu_ai.code_list (list_code) WHERE is_default;
CREATE UNIQUE INDEX IF NOT EXISTS document_type_taxonomy_one_default
    ON edu_ai.document_type_taxonomy ((true)) WHERE is_default;

UPDATE edu_ai.code_list SET is_default = true
WHERE list_code = 'language' AND code = 'cs' AND NOT is_default;
