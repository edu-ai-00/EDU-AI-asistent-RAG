-- last change in code by Martin Safranek (2026-08-02).
-- 98_settings_per_organization.sql
-- A settings row belongs to ONE top-level organisation and applies to its whole
-- tree; a row with legal_entity_code IS NULL is the global default used by every
-- organisation that has no row of its own.
--
-- ── why the OWNER is the root, never a unit ─────────────────────────────────
-- Settings hold things that must be the same for everyone who shares data: the
-- visibility policy (a unit having its own entity_visible_to_parent would let it
-- decide what its parent sees), the retrieval pipeline, the normalization
-- engines (which must match how the organisation's documents were indexed).
-- A trigger enforces it — a CHECK cannot look into legal_entity, and an
-- unenforced convention here would surface as "why does this school ignore its
-- settings" much later.
--
-- ── resolution (one query, shared by search and the chunking components) ────
--     root  := the top of the entity's parent chain
--     row   := active row of root, else the active global row
-- implemented as ORDER BY legal_entity_code NULLS LAST LIMIT 1 over the two
-- candidates, so the organisation's own row wins when both exist.
--
-- ── uniqueness ──────────────────────────────────────────────────────────────
-- One active row PER SCOPE: the old settings_one_active (one active row in the
-- whole table) becomes UNIQUE (legal_entity_code) NULLS NOT DISTINCT WHERE
-- is_active — one active global row AND one active row per organisation.
--
-- Existing rows keep legal_entity_code NULL: today's behaviour (one config for
-- everyone) continues unchanged until an organisation gets a row of its own.
--
-- Idempotent; transactional.

BEGIN;

ALTER TABLE edu_ai.settings
    ADD COLUMN IF NOT EXISTS legal_entity_code text
        REFERENCES edu_ai.legal_entity (legal_entity_code);

COMMENT ON COLUMN edu_ai.settings.legal_entity_code IS
    'NULL = the global default. Otherwise the TOP-LEVEL organisation this row '
    'belongs to (enforced by trigger); it applies to the whole tree under it.';

-- only a root entity may own settings
CREATE OR REPLACE FUNCTION edu_ai.settings_owner_must_be_root()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.legal_entity_code IS NOT NULL AND EXISTS (
        SELECT 1 FROM edu_ai.legal_entity
        WHERE legal_entity_code = NEW.legal_entity_code
          AND parent_code IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'settings.legal_entity_code must be a TOP-LEVEL entity '
                        '(% has a parent); settings apply to the whole '
                        'organisation, a unit cannot have its own',
                        NEW.legal_entity_code;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS settings_owner_root ON edu_ai.settings;
CREATE TRIGGER settings_owner_root
    BEFORE INSERT OR UPDATE OF legal_entity_code ON edu_ai.settings
    FOR EACH ROW EXECUTE FUNCTION edu_ai.settings_owner_must_be_root();

-- one active row per scope (global counts as a scope of its own)
DROP INDEX IF EXISTS edu_ai.settings_one_active;
CREATE UNIQUE INDEX IF NOT EXISTS settings_one_active_per_scope
    ON edu_ai.settings (legal_entity_code) NULLS NOT DISTINCT
    WHERE is_active;

COMMIT;
