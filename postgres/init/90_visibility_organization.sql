-- last change in code by Martin Safranek (2026-08-02).
-- 90_visibility_organization.sql
-- Visibility becomes four explicit levels instead of three plus implicit
-- inheritance.
--
--   private        only the user who ingested it
--   entity         only the exact entity the ingesting user belongs to
--   organization   every user of the top entity and of all entities under it
--   public         every entity on the platform
--
-- What changes and why it is better. Migration 89 gave a unit access to its
-- ancestors' `entity` documents automatically — inheritance as a property of the
-- hierarchy. That was implicit: whoever uploaded an organisation-level document
-- could not tell, from the choice they made, who would end up seeing it, and a
-- document meant for the head office alone had no way to stay there.
--
-- Now the uploader says it. `entity` means EXACTLY that entity — a document of the
-- primary school is not visible to the legal person above it, nor to the secondary
-- school beside it. `organization` means the whole tree of the legal person,
-- upward and downward. So the school rules covering both schools are ingested as
-- `organization`, and the head office's internal note stays `entity`.
--
-- `public` stays as the fourth level: it crosses the LEGAL PERSON boundary (the
-- CERMAT and MŠMT material that is true for every school), which is a different
-- question from anything inside one organisation, and only an entity holding the
-- right to publish may use it.
--
-- No data is rewritten. Every existing row is `entity` or `private`, and both keep
-- their meaning — `entity` simply stops being inherited, which is a narrowing, not
-- a widening. Nothing that was hidden becomes visible.
--
-- Idempotent; transactional.

BEGIN;

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['document_metadata', 'document_chunks', 'document_tables',
                             'qa_entries', 'qa_variants', 'qa_embedding',
                             'qa_pack_manifest']
    LOOP
        EXECUTE format(
            'ALTER TABLE edu_ai.%I DROP CONSTRAINT IF EXISTS %I', t, t || '_visibility_check');
        -- the older constraints carried hand-written names; drop those too
        EXECUTE format(
            'ALTER TABLE edu_ai.%I DROP CONSTRAINT IF EXISTS %I', t,
            CASE t WHEN 'document_metadata' THEN 'metadata_visibility_check'
                   WHEN 'document_chunks'  THEN 'chunks_visibility_check'
                   WHEN 'document_tables'  THEN 'tables_visibility_check'
                   WHEN 'qa_pack_manifest' THEN 'qa_pack_visibility_check'
                   ELSE t || '_visibility_check' END);
        EXECUTE format(
            'ALTER TABLE edu_ai.%I ADD CONSTRAINT %I CHECK (visibility IN '
            '(''private'', ''entity'', ''organization'', ''public''))',
            t, t || '_visibility_check');
    END LOOP;
END $$;

COMMENT ON COLUMN edu_ai.document_metadata.visibility IS
    'private (the uploader only) | entity (exactly that entity) | organization '
    '(the whole tree of the legal person) | public (every entity). Set at ingest; '
    'never inherited implicitly — see migration 90.';

COMMIT;
