-- last change in code by Martin Safranek (2026-08-18).
-- 146: the four QA tables get ONE column order, and tags loses its `source` key.
--
-- WHY A REBUILD. Postgres cannot reorder columns in place, so the tables are
-- dropped and recreated. That DELETES all QA content — deliberate and agreed
-- (Martin, 2026-08-18): the packs are re-ingested from the curator's JSON, which
-- is the source of truth, so nothing irreplaceable lives here. Do NOT apply this
-- to a database whose packs cannot be re-ingested.
--
-- WHY IT IS SAFE FOR THE CODE. Verified before writing this: no SELECT * on any
-- QA table anywhere (search, gateway, flows, tools), every INSERT names its
-- columns, and the two functions that read these tables (qa_duplicate_questions,
-- qa_pack_drift) reference columns by name. Column order is therefore
-- presentation only — it changes what `select * from qa_variants` looks like to a
-- human, nothing else.
--
-- THE ORDER, identical in all four tables:
--   1. identity        who this row is
--   2. content         what it says (the part that differs per table)
--   3. classification  ročník / level / předmět / doména / binding
--   4. access          entity / visibility / owner / roles
--   5. validity        valid_from / valid_to / is_active
--   6. provenance      setting / processing / file / created_at
-- Reading the four tables side by side now shows the same skeleton, and the
-- denormalised block (3+4+5) sits in the same place on the declaration and on
-- both scan surfaces — which is exactly the invariant migrations 140/144 are
-- about.
--
-- tags: `source` was the uploaded file's name, which file_uuid already
-- identifies. Only `category` stays — curator content from the pack JSON, read
-- by the device-pack export. Nothing to migrate: the rebuild empties the table
-- and the ingest writes the new shape.
--
-- Two legacy artefacts are NOT recreated, with reasons:
--   * qa_entries_public_idx  — partial index WHERE visibility = 'public', but the
--     visibility CHECK allows only private|entity|organization, so the predicate
--     can never match a row. Dead since visibility was narrowed.
--   * the pkey/sequence suffixes qa_variants_pkey1 / qa_variants_variant_id_seq1
--     — artefacts of an earlier rebuild. Nothing references either name.

SET search_path TO edu_ai, public;

BEGIN;

-- dependency order: embedding -> variants -> entries; the manifest is referenced
-- by nothing (entries carry pack_id as plain text, deliberately).
DROP TABLE IF EXISTS qa_embedding;
DROP TABLE IF EXISTS qa_variants;
DROP TABLE IF EXISTS qa_entries;
DROP TABLE IF EXISTS qa_pack_manifest;

-- ════════════════════════════════════════════════════════════════════════════
-- 1) qa_pack_manifest — the DECLARATION: what the pack is and who it is for
-- ════════════════════════════════════════════════════════════════════════════
CREATE TABLE qa_pack_manifest (
    -- identity
    pack_id            text    NOT NULL,
    lang               text    NOT NULL,
    version            integer NOT NULL,
    schema_version     integer NOT NULL,
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
    -- provenance
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

-- ════════════════════════════════════════════════════════════════════════════
-- 2) qa_entries — the ANSWER (payload; never scanned for retrieval)
-- ════════════════════════════════════════════════════════════════════════════
CREATE TABLE qa_entries (
    -- identity
    entry_id           uuid    NOT NULL PRIMARY KEY,
    pack_id            text    NOT NULL,
    -- content
    canonical_q        text    NOT NULL,
    answer_md          text    NOT NULL,
    tags               jsonb,
    asset_refs         jsonb,
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
    -- provenance
    processing_uuid    uuid,
    file_uuid          uuid,
    created_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT qa_entries_visibility_check
        CHECK (visibility = ANY (ARRAY['private','entity','organization'])),
    CONSTRAINT qa_entries_private_has_owner
        CHECK (visibility <> 'private' OR owner_user_id IS NOT NULL),
    CONSTRAINT qa_entries_owner_fkey
        FOREIGN KEY (legal_entity_code, owner_user_id)
        REFERENCES staff_user(legal_entity_code, user_id)
);

CREATE INDEX qa_entries_active_idx            ON qa_entries (is_active);
CREATE INDEX qa_entries_pack_idx              ON qa_entries (pack_id);
CREATE INDEX qa_entries_legal_entity_idx      ON qa_entries (legal_entity_code);
CREATE INDEX qa_entries_entity_visibility_idx ON qa_entries (legal_entity_code, visibility);
CREATE INDEX qa_entries_file_uuid_idx         ON qa_entries (file_uuid);

COMMENT ON COLUMN qa_entries.tags IS
    'Curator metadata from the pack JSON. Holds `category` only — the `source` key was dropped in 146 as a second copy of what file_uuid already says. Read by the device-pack export.';
COMMENT ON COLUMN qa_entries.visibility IS
    'private (the importer only — a pack under test) | entity | organization. Matches the CHECK; the older comment still said "public", which the CHECK has not allowed for a long time.';
COMMENT ON COLUMN qa_entries.owner_user_id IS
    'The staff user who imported the entry. Written on EVERY row: required for visibility = private, provenance for the rest.';
COMMENT ON COLUMN qa_entries.school_grade_ids IS
    'EFFECTIVE ročníky of this answer. Inherited from the pack at build time; a curator may override per entry. Same relationship as document_metadata.school_grade_user -> document_chunks.school_grade_ids (migration 138): the declaration fills in only what the row does not state itself.';

-- ════════════════════════════════════════════════════════════════════════════
-- 3) qa_variants — the LEXICAL scan surface (qa_exact / qa_lexical)
-- ════════════════════════════════════════════════════════════════════════════
CREATE TABLE qa_variants (
    -- identity
    variant_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entry_id           uuid    NOT NULL REFERENCES qa_entries(entry_id) ON DELETE CASCADE,
    -- content: the phrasing plus its three reduced forms and their tsvectors
    phrasing           text    NOT NULL,
    norm_text          text    NOT NULL,
    lemma_text         text    NOT NULL DEFAULT '',
    stem_text          text    NOT NULL DEFAULT '',
    ts_norm            tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, norm_text))  STORED,
    ts_lemma           tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, lemma_text)) STORED,
    ts_stem            tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, stem_text))  STORED,
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
    -- validity (no is_active: activation lives on the entry, which this table
    -- always joins; qa_embedding needs its own copy only for the partial index)
    valid_from         date,
    valid_to           date,
    -- provenance
    setting_id         uuid REFERENCES settings(setting_id) ON UPDATE CASCADE,
    processing_uuid    uuid,
    file_uuid          uuid,
    created_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT qa_variants_visibility_check
        CHECK (visibility = ANY (ARRAY['private','entity','organization']))
);

CREATE INDEX qa_variants_ts_norm_gin              ON qa_variants USING gin (ts_norm);
CREATE INDEX qa_variants_ts_lemma_gin             ON qa_variants USING gin (ts_lemma);
CREATE INDEX qa_variants_ts_stem_gin              ON qa_variants USING gin (ts_stem);
CREATE INDEX qa_variants_norm_trgm                ON qa_variants USING gin (norm_text edu_ai.gin_trgm_ops);
CREATE INDEX qa_variants_entry_idx                ON qa_variants (entry_id);
CREATE INDEX qa_variants_setting_idx              ON qa_variants (setting_id);
CREATE INDEX qa_variants_legal_entity_idx         ON qa_variants (legal_entity_code);
CREATE INDEX qa_variants_entity_visibility_idx    ON qa_variants (legal_entity_code, visibility);
CREATE INDEX qa_variants_audience_roles_idx       ON qa_variants USING gin (audience_roles);
CREATE INDEX qa_variants_school_subject_ids_gin   ON qa_variants USING gin (school_subject_ids);
CREATE INDEX qa_variants_domain_ids_gin           ON qa_variants USING gin (domain_ids);

COMMENT ON COLUMN qa_variants.school_grade_ids IS
    'Copy of qa_entries.school_grade_ids. Denormalised because qa_exact/qa_lexical SCAN this table — see scope.py: a predicate that lived only on the joined entry would be applied after ranking.';
COMMENT ON COLUMN qa_variants.binding_id IS
    'taxonomy_binding of the pack, denormalised for parity with document_chunks (migration 144).';

-- ════════════════════════════════════════════════════════════════════════════
-- 4) qa_embedding — the VECTOR scan surface (qa_vector)
-- ════════════════════════════════════════════════════════════════════════════
CREATE TABLE qa_embedding (
    -- identity
    variant_id          bigint NOT NULL PRIMARY KEY
                        REFERENCES qa_variants(variant_id) ON DELETE CASCADE,
    entry_id            uuid   NOT NULL,
    -- content: the vector and the generation that produced it
    embedding           vector(1536),
    embedding_model     text,
    embedding_dimension integer,
    embedding_provider  text,
    -- classification
    school_grade_ids    bigint[],
    school_level        text,
    school_subject_ids  bigint[],
    domain_ids          bigint[],
    binding_id          bigint REFERENCES taxonomy_binding(id),
    -- access
    legal_entity_code   text REFERENCES legal_entity(legal_entity_code),
    visibility          text    NOT NULL DEFAULT 'entity',
    owner_user_id       text,
    audience_roles      text[],
    -- validity
    valid_from          date,
    valid_to            date,
    is_active           boolean NOT NULL DEFAULT true,
    -- provenance
    processing_uuid     uuid,
    file_uuid           uuid,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT qa_embedding_visibility_check
        CHECK (visibility = ANY (ARRAY['private','entity','organization']))
);

CREATE INDEX qa_embedding_hnsw_active ON qa_embedding
    USING hnsw (embedding vector_cosine_ops) WHERE is_active;
CREATE INDEX qa_embedding_entry_idx              ON qa_embedding (entry_id);
CREATE INDEX qa_embedding_legal_entity_idx       ON qa_embedding (legal_entity_code);
CREATE INDEX qa_embedding_entity_visibility_idx  ON qa_embedding (legal_entity_code, visibility);
CREATE INDEX qa_embedding_audience_roles_idx     ON qa_embedding USING gin (audience_roles);
CREATE INDEX qa_embedding_school_subject_ids_gin ON qa_embedding USING gin (school_subject_ids);
CREATE INDEX qa_embedding_domain_ids_gin         ON qa_embedding USING gin (domain_ids);

COMMENT ON COLUMN qa_embedding.visibility IS
    'Copy of the entry''s visibility. qa_vector walks the nearest-neighbour index and joins per candidate, so the filter has to be here to stop candidates at the index rather than after it.';
COMMENT ON COLUMN qa_embedding.school_grade_ids IS
    'Copy of qa_entries.school_grade_ids. Denormalised because the nearest-neighbour scan orders by `embedding <=> query` and joins per candidate — the same reason legal_entity_code/visibility already live here.';
COMMENT ON COLUMN qa_embedding.binding_id IS
    'taxonomy_binding of the pack, denormalised — the ANN scan filters here, not on qa_entries (migration 144).';
COMMENT ON COLUMN qa_embedding.embedding_dimension IS
    'Width of the stored vector, measured at insert time — never assumed from the registry, so a generation swap cannot leave the column lying about the data.';

COMMIT;
