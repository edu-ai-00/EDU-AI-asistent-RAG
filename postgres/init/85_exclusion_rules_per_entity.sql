-- last change in code by Martin Safranek (2026-08-02).
-- 85_exclusion_rules_per_entity.sql
-- A chunk-exclusion rule belongs to a legal entity and fires only for that
-- entity's ingests.
--
-- Why it matters more than it looks: these rules DROP chunks before they are
-- embedded, so a rule written for one entity's documents ("skip the page footer
-- that starts with 'ZŠ Nová Ves, Město 1'") would silently delete content from
-- another entity's document. Not a wrong answer — missing text, with nothing in
-- the index to show it was ever there.
--
-- Nullable, and NULL keeps its meaning as "applies to everyone": a rule that drops
-- an empty chunk or a page number is not entity-specific and should not have to be
-- written once per entity. So the filter reads
--
--     WHERE is_active AND (legal_entity_code IS NULL OR legal_entity_code = %s)
--
-- and the entity comes from the row being ingested, i.e. from the user who started
-- the ingest. Ordering across the two groups stays rule_order, so an entity can
-- deliberately slot its own rule between the global ones.
--
-- If a deployment ever wants "entity rules ONLY, no global ones", that is a change
-- in the component's WHERE clause, not in this table.
--
-- Idempotent; transactional.

BEGIN;

ALTER TABLE edu_ai.chunk_exclusion_rule
    ADD COLUMN IF NOT EXISTS legal_entity_code text
        REFERENCES edu_ai.legal_entity (legal_entity_code);

COMMENT ON COLUMN edu_ai.chunk_exclusion_rule.legal_entity_code IS
    'The entity whose ingests this rule fires for. NULL = every entity. A rule '
    'drops chunks before embedding, so a misapplied one loses content silently — '
    'see migration 85.';

CREATE INDEX IF NOT EXISTS chunk_exclusion_rule_entity_idx
    ON edu_ai.chunk_exclusion_rule (legal_entity_code) WHERE is_active;

COMMIT;
