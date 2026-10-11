-- last change in code by Martin Safranek (2026-08-02).
-- 78_document_visibility.sql
-- How far a document reaches: private / entity / public.
--
-- Chosen at ingest. `private` is the uploader's own, `entity` is everyone in the
-- legal entity, `public` is everyone on the platform — the school act, a public
-- health notice, the CERMAT rules. This is what makes a genuinely shared corpus
-- possible without the pseudo-entity the design first reached for.
--
-- ── ownership and reach are two axes ────────────────────────────────────────
-- legal_entity_code says WHO OWNS the document and is required even for a public
-- one — somebody uploaded the school act, and the provenance is worth keeping.
-- visibility says HOW FAR it reaches. Keeping them apart is what lets
-- legal_entity_code become NOT NULL later without carving out an exception.
--
--     visibility = 'public'
--  OR (visibility = 'entity'  AND legal_entity_code = :entity)
--  OR (visibility = 'private' AND legal_entity_code = :entity
--                             AND owner_user_id = :user)
--
-- ── the columns go on the chunks too, on purpose ────────────────────────────
-- Denormalised, so retrieval needs no join. It is not only convenience:
-- rag_vector orders by `embedding <=> query` BEFORE any join to metadata, so a
-- predicate that lives only on metadata is applied after the whole table has been
-- ranked. On chunks it is applied during the scan.
--
-- ── 'public' did NOT mean public ────────────────────────────────────────────
-- private_public_flag already exists (default 'private') and search reads it in
-- exactly zero places — it has never been enforced. Note the mapping when
-- converting existing rows: in a single-tenant world 'public' meant "everyone in
-- this school", which is 'entity' here, NOT 'public'. Translating it literally
-- would publish every document of every entity to every other one.
--
-- The old column is left in place: nine insert components across the six flows
-- list it among their required columns, and dropping it would break them. It
-- becomes redundant once they write `visibility`.
--
-- ── default 'entity' ────────────────────────────────────────────────────────
-- Not 'private': a private document with no owner is reachable by nobody, so a
-- forgotten value would silently lose documents. Not 'public' for obvious
-- reasons. 'entity' is the pre-multi-tenant behaviour and cannot leak across
-- entities, so it is the safe thing to fall back to while the flows learn to set
-- it explicitly.
--
-- QA is deliberately NOT included: whether a QA pack can be private to one person
-- is still open. Documents first.
--
-- Idempotent; transactional.

BEGIN;

-- ── authoritative on the document ───────────────────────────────────────────
ALTER TABLE edu_ai.edu_ai_document_metadata
    ADD COLUMN IF NOT EXISTS visibility text NOT NULL DEFAULT 'entity',
    ADD COLUMN IF NOT EXISTS owner_user_id text;

ALTER TABLE edu_ai.edu_ai_document_metadata
    DROP CONSTRAINT IF EXISTS metadata_visibility_check;
ALTER TABLE edu_ai.edu_ai_document_metadata
    ADD CONSTRAINT metadata_visibility_check
        CHECK (visibility IN ('private', 'entity', 'public'));

-- a private document without an owner is reachable by nobody
ALTER TABLE edu_ai.edu_ai_document_metadata
    DROP CONSTRAINT IF EXISTS metadata_private_has_owner;
ALTER TABLE edu_ai.edu_ai_document_metadata
    ADD CONSTRAINT metadata_private_has_owner
        CHECK (visibility <> 'private' OR owner_user_id IS NOT NULL);

-- the owner is a staff user OF THAT ENTITY. MATCH SIMPLE: while
-- legal_entity_code is still nullable the pair is simply unchecked, which is what
-- lets this land before the flows carry the entity.
ALTER TABLE edu_ai.edu_ai_document_metadata
    DROP CONSTRAINT IF EXISTS metadata_owner_fkey;
ALTER TABLE edu_ai.edu_ai_document_metadata
    ADD CONSTRAINT metadata_owner_fkey
        FOREIGN KEY (legal_entity_code, owner_user_id)
        REFERENCES edu_ai.staff_user (legal_entity_code, user_id);

COMMENT ON COLUMN edu_ai.edu_ai_document_metadata.visibility IS
    'How far the document reaches: private (the owner only) | entity (everyone in '
    'legal_entity_code) | public (every entity). Replaces private_public_flag, '
    'which search never enforced — see migration 78.';
COMMENT ON COLUMN edu_ai.edu_ai_document_metadata.owner_user_id IS
    'Required for visibility = private; the staff user who ingested it.';

-- ── denormalised onto the search path ───────────────────────────────────────
ALTER TABLE edu_ai.edu_ai_document_chunks
    ADD COLUMN IF NOT EXISTS visibility text NOT NULL DEFAULT 'entity',
    ADD COLUMN IF NOT EXISTS owner_user_id text;

ALTER TABLE edu_ai.edu_ai_document_chunks
    DROP CONSTRAINT IF EXISTS chunks_visibility_check;
ALTER TABLE edu_ai.edu_ai_document_chunks
    ADD CONSTRAINT chunks_visibility_check
        CHECK (visibility IN ('private', 'entity', 'public'));

COMMENT ON COLUMN edu_ai.edu_ai_document_chunks.visibility IS
    'Copy of the document''s visibility, so retrieval filters during the scan '
    'instead of after the join — rag_vector ranks the whole table before any join '
    'would apply. Written by ingestion; the document row is authoritative.';

ALTER TABLE edu_ai.edu_ai_document_tables
    ADD COLUMN IF NOT EXISTS visibility text NOT NULL DEFAULT 'entity',
    ADD COLUMN IF NOT EXISTS owner_user_id text;

ALTER TABLE edu_ai.edu_ai_document_tables
    DROP CONSTRAINT IF EXISTS tables_visibility_check;
ALTER TABLE edu_ai.edu_ai_document_tables
    ADD CONSTRAINT tables_visibility_check
        CHECK (visibility IN ('private', 'entity', 'public'));

-- ── indexes for the three branches of the predicate ─────────────────────────
-- The predicate is an OR, so the planner builds a bitmap of the branches; each
-- branch gets the index it needs and the public one is deliberately partial —
-- public documents are the minority and are read by everybody.
CREATE INDEX IF NOT EXISTS chunks_entity_visibility_idx
    ON edu_ai.edu_ai_document_chunks (legal_entity_code, visibility);
CREATE INDEX IF NOT EXISTS chunks_public_idx
    ON edu_ai.edu_ai_document_chunks (id) WHERE visibility = 'public';
CREATE INDEX IF NOT EXISTS chunks_owner_idx
    ON edu_ai.edu_ai_document_chunks (legal_entity_code, owner_user_id)
    WHERE visibility = 'private';
CREATE INDEX IF NOT EXISTS metadata_entity_visibility_idx
    ON edu_ai.edu_ai_document_metadata (legal_entity_code, visibility);

COMMIT;
