-- last change in code by Martin Safranek (2026-08-02).
-- 91_drop_public_visibility.sql
-- `public` goes. Visibility never crosses a legal person.
--
--   private        only the user who ingested it
--   entity         only the exact entity they belong to
--   organization   the whole tree of that legal person
--
-- The level existed for material that is true everywhere — the CERMAT and MŠMT
-- documents on admission to secondary school. Those are now ingested per
-- organisation instead: into one unit if only that unit needs them, or onto the
-- legal person if the whole organisation does.
--
-- The cost is duplication — N organisations means N copies of the same PDF and N
-- times the embedding — and it is worth naming, because it grows linearly with
-- customers. What it buys is that a document can never reach an organisation that
-- did not ingest it. No cross-tenant predicate, no "who may publish platform-wide"
-- right to grant and audit, no vocabulary that has to agree across tenants for a
-- filter not to hide someone else's document. The isolation boundary and the legal
-- person become the same line, with no exception carved through it.
--
-- Nothing to migrate: no row anywhere carries 'public' (checked before running).
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
        EXECUTE format('UPDATE edu_ai.%I SET visibility = ''organization'' '
                       'WHERE visibility = ''public''', t);
        EXECUTE format('ALTER TABLE edu_ai.%I DROP CONSTRAINT IF EXISTS %I',
                       t, t || '_visibility_check');
        EXECUTE format(
            'ALTER TABLE edu_ai.%I ADD CONSTRAINT %I CHECK (visibility IN '
            '(''private'', ''entity'', ''organization''))', t, t || '_visibility_check');
    END LOOP;
END $$;

COMMENT ON COLUMN edu_ai.document_metadata.visibility IS
    'private (the uploader only) | entity (exactly that entity) | organization '
    '(the whole tree of the legal person). Never crosses a legal person — shared '
    'material is ingested per organisation. See migration 91.';

COMMIT;
