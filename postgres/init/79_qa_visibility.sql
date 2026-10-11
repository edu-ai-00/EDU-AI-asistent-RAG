-- last change in code by Martin Safranek (2026-08-02).
-- 79_qa_visibility.sql
-- The same three-way reach for QA as for documents (migration 78):
-- private / entity / public.
--
-- Each value earns its place here:
--   private   a pack being tested. Somebody imports it, checks the answers it
--             produces, and nobody else's search is disturbed while they do.
--             This is the value QA needs most and documents need least.
--   entity    the ordinary case — the school's own curated answers.
--   public    an FAQ that is true everywhere (the entrance-exam procedure).
--
-- ── where the columns go, and why not only on qa_entries ────────────────────
-- qa_entries is authoritative. qa_variants and qa_embedding get a copy for the
-- same reason chunks did: qa_vector runs
--
--     FROM qa_embedding q JOIN qa_entries e ON e.entry_id = q.entry_id
--     ORDER BY q.embedding <=> %s::vector LIMIT 1
--
-- so a predicate that lives only on qa_entries is evaluated per candidate row
-- while the nearest-neighbour scan walks outwards — with a selective filter that
-- walk can be long. On the embedding row it stops candidates at the index.
--
-- qa_pack_manifest gets it as the pack's declared default, which is what the
-- import applies to the entries it creates.
--
-- ── the owner is the person who imported it ─────────────────────────────────
-- Composite foreign key to staff_user, so a private pack of entity A cannot be
-- owned by somebody from entity B. MATCH SIMPLE, so while legal_entity_code is
-- still nullable the pair is simply unchecked.
--
-- Default 'entity' for the same reason as documents: it is the pre-multi-tenant
-- behaviour, it cannot leak across entities, and unlike 'private' it does not
-- silently make rows unreachable when a writer forgets to set it.
--
-- Idempotent; transactional.

BEGIN;

-- ── authoritative ───────────────────────────────────────────────────────────
ALTER TABLE edu_ai.qa_entries
    ADD COLUMN IF NOT EXISTS visibility text NOT NULL DEFAULT 'entity',
    ADD COLUMN IF NOT EXISTS owner_user_id text;

ALTER TABLE edu_ai.qa_entries DROP CONSTRAINT IF EXISTS qa_entries_visibility_check;
ALTER TABLE edu_ai.qa_entries
    ADD CONSTRAINT qa_entries_visibility_check
        CHECK (visibility IN ('private', 'entity', 'public'));

ALTER TABLE edu_ai.qa_entries DROP CONSTRAINT IF EXISTS qa_entries_private_has_owner;
ALTER TABLE edu_ai.qa_entries
    ADD CONSTRAINT qa_entries_private_has_owner
        CHECK (visibility <> 'private' OR owner_user_id IS NOT NULL);

ALTER TABLE edu_ai.qa_entries DROP CONSTRAINT IF EXISTS qa_entries_owner_fkey;
ALTER TABLE edu_ai.qa_entries
    ADD CONSTRAINT qa_entries_owner_fkey
        FOREIGN KEY (legal_entity_code, owner_user_id)
        REFERENCES edu_ai.staff_user (legal_entity_code, user_id);

COMMENT ON COLUMN edu_ai.qa_entries.visibility IS
    'private (the importer only — a pack under test) | entity | public. See '
    'migration 79.';

-- ── copies on the search path ───────────────────────────────────────────────
ALTER TABLE edu_ai.qa_variants
    ADD COLUMN IF NOT EXISTS visibility text NOT NULL DEFAULT 'entity',
    ADD COLUMN IF NOT EXISTS owner_user_id text;
ALTER TABLE edu_ai.qa_variants DROP CONSTRAINT IF EXISTS qa_variants_visibility_check;
ALTER TABLE edu_ai.qa_variants
    ADD CONSTRAINT qa_variants_visibility_check
        CHECK (visibility IN ('private', 'entity', 'public'));

ALTER TABLE edu_ai.qa_embedding
    ADD COLUMN IF NOT EXISTS visibility text NOT NULL DEFAULT 'entity',
    ADD COLUMN IF NOT EXISTS owner_user_id text;
ALTER TABLE edu_ai.qa_embedding DROP CONSTRAINT IF EXISTS qa_embedding_visibility_check;
ALTER TABLE edu_ai.qa_embedding
    ADD CONSTRAINT qa_embedding_visibility_check
        CHECK (visibility IN ('private', 'entity', 'public'));

COMMENT ON COLUMN edu_ai.qa_embedding.visibility IS
    'Copy of the entry''s visibility. qa_vector walks the nearest-neighbour index '
    'and joins per candidate, so the filter has to be here to stop candidates at '
    'the index rather than after it.';

-- ── the pack declares the default its entries get ───────────────────────────
ALTER TABLE edu_ai.qa_pack_manifest
    ADD COLUMN IF NOT EXISTS visibility text NOT NULL DEFAULT 'entity',
    ADD COLUMN IF NOT EXISTS owner_user_id text;
ALTER TABLE edu_ai.qa_pack_manifest DROP CONSTRAINT IF EXISTS qa_pack_visibility_check;
ALTER TABLE edu_ai.qa_pack_manifest
    ADD CONSTRAINT qa_pack_visibility_check
        CHECK (visibility IN ('private', 'entity', 'public'));

COMMENT ON COLUMN edu_ai.qa_pack_manifest.visibility IS
    'The reach the import gives the entries it creates. A pack under test is '
    'imported as private and promoted once its answers check out.';

-- ── indexes, one per branch of the predicate ────────────────────────────────
CREATE INDEX IF NOT EXISTS qa_entries_entity_visibility_idx
    ON edu_ai.qa_entries (legal_entity_code, visibility);
CREATE INDEX IF NOT EXISTS qa_embedding_entity_visibility_idx
    ON edu_ai.qa_embedding (legal_entity_code, visibility);
CREATE INDEX IF NOT EXISTS qa_variants_entity_visibility_idx
    ON edu_ai.qa_variants (legal_entity_code, visibility);
CREATE INDEX IF NOT EXISTS qa_entries_public_idx
    ON edu_ai.qa_entries (entry_id) WHERE visibility = 'public';

COMMIT;
