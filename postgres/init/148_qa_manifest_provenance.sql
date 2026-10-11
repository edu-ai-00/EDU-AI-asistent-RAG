-- last change in code by Martin Safranek (2026-08-18).
-- 148: qa_pack_manifest gets processing_uuid + file_uuid, right behind identity.
--
-- Migration 147 left the manifest out on the argument that provenance belongs to
-- the rows and not to the declaration. That was wrong (Martin, 2026-08-18): a
-- manifest row IS one upload. Each version is created by exactly one ingest, so
-- it has exactly one processing_uuid and one file_uuid — proven on the live data,
-- where every manifest version (including the deactivated one) resolves to a
-- single pair. The declaration was in fact the ONE place where "which file did
-- this come from?" could not be answered without walking the entries.
--
-- Placement follows the other three tables: identity -> provenance -> the rest.
-- Here the identity block is pack_id + lang + version + schema_version, so the
-- two uuids land at positions 5 and 6.
--
-- Rebuild rather than ALTER ADD, because ALTER appends to the end and the point
-- is the position. Data is preserved (packs were being tested when this was
-- asked): copied out, copied back BY NAME. Nothing references qa_pack_manifest —
-- entries carry pack_id as plain text, deliberately — so no FK has to be dropped.
--
-- BACKFILL is exact, not a guess: the manifest's built_at and its entries'
-- created_at are the SAME ingest timestamp (both come from one `ingest_ts` in the
-- insert component), so that equality names the upload that produced each
-- version. Verified on all four existing rows: one distinct uuid pair each.

SET search_path TO edu_ai, public;

BEGIN;

CREATE TABLE qa_pack_manifest_bak148 AS SELECT * FROM qa_pack_manifest;

DROP TABLE qa_pack_manifest;

CREATE TABLE qa_pack_manifest (
    -- identity
    pack_id            text    NOT NULL,
    lang               text    NOT NULL,
    version            integer NOT NULL,
    schema_version     integer NOT NULL,
    -- provenance (migration 148: one manifest version = one upload)
    processing_uuid    uuid,
    file_uuid          uuid,
    -- classification
    school_grade_ids   bigint[],
    school_level       text,
    school_subject_ids bigint[],
    domain_ids         bigint[],
    binding_id         bigint REFERENCES taxonomy_binding(id),
    -- access
    legal_entity_code  text REFERENCES legal_entity(legal_entity_code),
    visibility         text    NOT NULL DEFAULT 'entity',
    owner_user_id      text,
    audience_roles     text[],
    -- validity
    valid_from         date,
    valid_to           date,
    is_active          boolean NOT NULL DEFAULT true,
    -- retrieval tuning (manifest only — the A/B/C bars of THIS pack)
    t_answer           real    NOT NULL,
    t_suggest          real    NOT NULL,
    m_margin           real    NOT NULL,
    -- audit
    entry_count        integer NOT NULL,
    built_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT qa_pack_manifest_visibility_check
        CHECK (visibility = ANY (ARRAY['private','entity','organization'])),
    CONSTRAINT qa_pack_manifest_school_level_check
        CHECK (school_level IS NULL OR school_level = ANY (ARRAY['MS','ZS','SS']))
);

CREATE INDEX qa_pack_manifest_active_idx ON qa_pack_manifest (is_active) WHERE is_active;
CREATE UNIQUE INDEX qa_pack_manifest_one_active_per_pack
    ON qa_pack_manifest (legal_entity_code, pack_id) NULLS NOT DISTINCT WHERE is_active;

COMMENT ON COLUMN qa_pack_manifest.processing_uuid IS
    'The ingest run that built THIS version of the pack (migration 148). One manifest version = one upload, so this is single-valued; it equals the processing_uuid on every entry whose created_at matches built_at.';
COMMENT ON COLUMN qa_pack_manifest.file_uuid IS
    'The uploaded pack JSON this version was built from (migration 148).';
COMMENT ON COLUMN qa_pack_manifest.visibility IS
    'The reach the import gives the entries it creates. A pack under test is imported as private and promoted once its answers check out.';
COMMENT ON COLUMN qa_pack_manifest.school_grade_ids IS
    'The pack''s ročníky as code_list ids of the owning entity — THE single truth (the scalar `grade` was dropped in 145). NULL/empty = all ročníky.';
COMMENT ON COLUMN qa_pack_manifest.school_subject_ids IS
    'The pack''s předměty as code_list ids — THE single truth (the scalar `subject` was dropped in 145). NULL = not subject-specific.';
COMMENT ON COLUMN qa_pack_manifest.domain_ids IS
    'Declared taxonomy_rules DOMAIN ids, resolved against the entity''s own catalog (edu_ai.resolved_taxonomy_rules). Mirrors document_metadata.document_domain_user.';
COMMENT ON COLUMN qa_pack_manifest.audience_roles IS
    'code_list(role) codes the pack is addressed to. Same hard-filter semantics as everywhere else; NULL/empty = no restriction at pack level, per-entry roles still apply.';
COMMENT ON COLUMN qa_pack_manifest.school_level IS
    'MS | ZS | SS — the school level this pack speaks to. Taken from the institution the pack is uploaded under (migration 144); the umbrella organisation has none and keeps NULL. qa_pack_drift() reports a disagreement with what grade_level() derives from school_grade_ids.';
COMMENT ON COLUMN qa_pack_manifest.binding_id IS
    'taxonomy_binding of the pack: domain x level x ročník set, derived exactly as a document''s binding is (migration 144). NULL when ambiguous or unmatched.';

INSERT INTO qa_pack_manifest (pack_id, lang, version, schema_version,
       school_grade_ids, school_level, school_subject_ids, domain_ids, binding_id,
       legal_entity_code, visibility, owner_user_id, audience_roles,
       valid_from, valid_to, is_active, t_answer, t_suggest, m_margin,
       entry_count, built_at)
SELECT pack_id, lang, version, schema_version,
       school_grade_ids, school_level, school_subject_ids, domain_ids, binding_id,
       legal_entity_code, visibility, owner_user_id, audience_roles,
       valid_from, valid_to, is_active, t_answer, t_suggest, m_margin,
       entry_count, built_at
  FROM qa_pack_manifest_bak148;

-- exact backfill: built_at = created_at names the upload behind each version
UPDATE qa_pack_manifest m
   SET processing_uuid = src.processing_uuid,
       file_uuid       = src.file_uuid
  FROM (SELECT DISTINCT e.pack_id, e.legal_entity_code, e.created_at,
               e.processing_uuid, e.file_uuid
          FROM qa_entries e) src
 WHERE src.pack_id           = m.pack_id
   AND src.legal_entity_code IS NOT DISTINCT FROM m.legal_entity_code
   AND src.created_at        = m.built_at;

DO $$
DECLARE a bigint; b bigint; missing bigint;
BEGIN
    SELECT (SELECT count(*) FROM qa_pack_manifest), (SELECT count(*) FROM qa_pack_manifest_bak148)
      INTO a, b;
    IF a <> b THEN RAISE EXCEPTION 'qa_pack_manifest: copied % of % rows', a, b; END IF;
    -- a version whose entries are gone cannot be backfilled; that is information,
    -- not a failure, so report instead of aborting
    SELECT count(*) INTO missing FROM qa_pack_manifest WHERE processing_uuid IS NULL;
    IF missing > 0 THEN
        RAISE NOTICE '% manifest row(s) kept NULL provenance (no entries with a matching built_at)', missing;
    END IF;
END $$;

DROP TABLE qa_pack_manifest_bak148;

COMMIT;
