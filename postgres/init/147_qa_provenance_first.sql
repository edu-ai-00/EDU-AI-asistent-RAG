-- last change in code by Martin Safranek (2026-08-18).
-- 147: processing_uuid + file_uuid move to the front, right behind the id.
--
-- Martin, 2026-08-18, while testing uploads: the two provenance uuids are what
-- you reach for FIRST when checking an ingest ("did this row come from the file I
-- just uploaded?"), so burying them at the tail of `select *` was wrong. They now
-- sit immediately after the identity columns, before the payload.
--
-- qa_pack_manifest is untouched — it has no processing_uuid/file_uuid at all
-- (that provenance belongs to the rows, not to the declaration).
--
-- Unlike migration 146 this does NOT empty the tables: an upload was in flight
-- when the change was requested. Data is copied out to plain backup tables, the
-- real tables are rebuilt in the new order, and the rows go back in by NAME —
-- so the copy is order-independent by construction. qa_variants.variant_id is an
-- IDENTITY column, so the ids are re-inserted with OVERRIDING SYSTEM VALUE (the
-- embedding rows reference them) and the sequence is advanced afterwards.

SET search_path TO edu_ai, public;

BEGIN;

CREATE TABLE qa_entries_bak147   AS SELECT * FROM qa_entries;
CREATE TABLE qa_variants_bak147  AS SELECT * FROM qa_variants;
CREATE TABLE qa_embedding_bak147 AS SELECT * FROM qa_embedding;

DROP TABLE qa_embedding;
DROP TABLE qa_variants;
DROP TABLE qa_entries;

-- ════════════════════════════════════════════════════════════════════════════
-- qa_entries — identity -> PROVENANCE -> content -> classification -> access
--              -> validity -> created_at
-- ════════════════════════════════════════════════════════════════════════════
CREATE TABLE qa_entries (
    -- identity
    entry_id           uuid    NOT NULL PRIMARY KEY,
    pack_id            text    NOT NULL,
    -- provenance (front, migration 147: the first thing you check on an ingest)
    processing_uuid    uuid,
    file_uuid          uuid,
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
    'private (the importer only — a pack under test) | entity | organization. Matches the CHECK.';
COMMENT ON COLUMN qa_entries.owner_user_id IS
    'The staff user who imported the entry. Written on EVERY row: required for visibility = private, provenance for the rest.';
COMMENT ON COLUMN qa_entries.school_grade_ids IS
    'EFFECTIVE ročníky of this answer. Inherited from the pack at build time; a curator may override per entry. Same relationship as document_metadata.school_grade_user -> document_chunks.school_grade_ids (migration 138): the declaration fills in only what the row does not state itself.';

-- ════════════════════════════════════════════════════════════════════════════
-- qa_variants — the LEXICAL scan surface
-- ════════════════════════════════════════════════════════════════════════════
CREATE TABLE qa_variants (
    -- identity
    variant_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entry_id           uuid    NOT NULL REFERENCES qa_entries(entry_id) ON DELETE CASCADE,
    -- provenance
    processing_uuid    uuid,
    file_uuid          uuid,
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
    -- the settings generation that produced the reduced forms above
    setting_id         uuid REFERENCES settings(setting_id) ON UPDATE CASCADE,
    created_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT qa_variants_visibility_check
        CHECK (visibility = ANY (ARRAY['private','entity','organization']))
);

CREATE INDEX qa_variants_ts_norm_gin            ON qa_variants USING gin (ts_norm);
CREATE INDEX qa_variants_ts_lemma_gin           ON qa_variants USING gin (ts_lemma);
CREATE INDEX qa_variants_ts_stem_gin            ON qa_variants USING gin (ts_stem);
CREATE INDEX qa_variants_norm_trgm              ON qa_variants USING gin (norm_text edu_ai.gin_trgm_ops);
CREATE INDEX qa_variants_entry_idx              ON qa_variants (entry_id);
CREATE INDEX qa_variants_setting_idx            ON qa_variants (setting_id);
CREATE INDEX qa_variants_legal_entity_idx       ON qa_variants (legal_entity_code);
CREATE INDEX qa_variants_entity_visibility_idx  ON qa_variants (legal_entity_code, visibility);
CREATE INDEX qa_variants_audience_roles_idx     ON qa_variants USING gin (audience_roles);
CREATE INDEX qa_variants_school_subject_ids_gin ON qa_variants USING gin (school_subject_ids);
CREATE INDEX qa_variants_domain_ids_gin         ON qa_variants USING gin (domain_ids);

COMMENT ON COLUMN qa_variants.school_grade_ids IS
    'Copy of qa_entries.school_grade_ids. Denormalised because qa_exact/qa_lexical SCAN this table — see scope.py: a predicate that lived only on the joined entry would be applied after ranking.';
COMMENT ON COLUMN qa_variants.binding_id IS
    'taxonomy_binding of the pack, denormalised for parity with document_chunks (migration 144).';

-- ════════════════════════════════════════════════════════════════════════════
-- qa_embedding — the VECTOR scan surface
-- ════════════════════════════════════════════════════════════════════════════
CREATE TABLE qa_embedding (
    -- identity
    variant_id          bigint NOT NULL PRIMARY KEY
                        REFERENCES qa_variants(variant_id) ON DELETE CASCADE,
    entry_id            uuid   NOT NULL,
    -- provenance
    processing_uuid     uuid,
    file_uuid           uuid,
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

-- ── data back in, BY NAME (parent first, so the FKs hold) ────────────────────
INSERT INTO qa_entries (entry_id, pack_id, processing_uuid, file_uuid, canonical_q,
       answer_md, tags, asset_refs, school_grade_ids, school_level,
       school_subject_ids, domain_ids, binding_id, legal_entity_code, visibility,
       owner_user_id, audience_roles, valid_from, valid_to, is_active, created_at)
SELECT entry_id, pack_id, processing_uuid, file_uuid, canonical_q,
       answer_md, tags, asset_refs, school_grade_ids, school_level,
       school_subject_ids, domain_ids, binding_id, legal_entity_code, visibility,
       owner_user_id, audience_roles, valid_from, valid_to, is_active, created_at
  FROM qa_entries_bak147;

-- ts_norm/ts_lemma/ts_stem are GENERATED: they are recomputed, never copied.
INSERT INTO qa_variants (variant_id, entry_id, processing_uuid, file_uuid, phrasing,
       norm_text, lemma_text, stem_text, school_grade_ids, school_level,
       school_subject_ids, domain_ids, binding_id, legal_entity_code, visibility,
       owner_user_id, audience_roles, valid_from, valid_to, setting_id, created_at)
OVERRIDING SYSTEM VALUE
SELECT variant_id, entry_id, processing_uuid, file_uuid, phrasing,
       norm_text, lemma_text, stem_text, school_grade_ids, school_level,
       school_subject_ids, domain_ids, binding_id, legal_entity_code, visibility,
       owner_user_id, audience_roles, valid_from, valid_to, setting_id, created_at
  FROM qa_variants_bak147;

-- the identity sequence must not hand out an id that already exists
SELECT setval(pg_get_serial_sequence('qa_variants','variant_id'),
              GREATEST((SELECT COALESCE(MAX(variant_id), 0) FROM qa_variants), 1));

INSERT INTO qa_embedding (variant_id, entry_id, processing_uuid, file_uuid, embedding,
       embedding_model, embedding_dimension, embedding_provider, school_grade_ids,
       school_level, school_subject_ids, domain_ids, binding_id, legal_entity_code,
       visibility, owner_user_id, audience_roles, valid_from, valid_to, is_active, created_at)
SELECT variant_id, entry_id, processing_uuid, file_uuid, embedding,
       embedding_model, embedding_dimension, embedding_provider, school_grade_ids,
       school_level, school_subject_ids, domain_ids, binding_id, legal_entity_code,
       visibility, owner_user_id, audience_roles, valid_from, valid_to, is_active, created_at
  FROM qa_embedding_bak147;

-- fail loudly rather than leave a silently short copy
DO $$
DECLARE a bigint; b bigint;
BEGIN
    SELECT (SELECT count(*) FROM qa_entries),   (SELECT count(*) FROM qa_entries_bak147)   INTO a, b;
    IF a <> b THEN RAISE EXCEPTION 'qa_entries: copied % of % rows', a, b; END IF;
    SELECT (SELECT count(*) FROM qa_variants),  (SELECT count(*) FROM qa_variants_bak147)  INTO a, b;
    IF a <> b THEN RAISE EXCEPTION 'qa_variants: copied % of % rows', a, b; END IF;
    SELECT (SELECT count(*) FROM qa_embedding), (SELECT count(*) FROM qa_embedding_bak147) INTO a, b;
    IF a <> b THEN RAISE EXCEPTION 'qa_embedding: copied % of % rows', a, b; END IF;
END $$;

DROP TABLE qa_entries_bak147;
DROP TABLE qa_variants_bak147;
DROP TABLE qa_embedding_bak147;

COMMIT;
