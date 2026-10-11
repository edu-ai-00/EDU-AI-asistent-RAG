-- last change in code by Martin Safranek (2026-08-18).
-- 149: qa_variants gets is_active, and its lexical indexes become partial.
--
-- The gap Martin spotted: qa_entries and qa_embedding carry is_active, qa_variants
-- did not. It was correct but not equal. qa_exact/qa_lexical scan qa_variants and
-- join qa_entries, whose `e.is_active` is in the WHERE — and because the join is
-- evaluated before the sort, a dead variant could never win a query. So this was
-- never a wrong-answer bug.
--
-- What it WAS: the vector surface excludes dead rows at the index
-- (qa_embedding_hnsw_active ... WHERE is_active) while the lexical surface indexed
-- every variant ever written. Measured before this migration: 91 of 271 variants
-- (34 %) belonged to deactivated entries and sat in all three ts_* GIN indexes
-- plus the trgm index. That share only grows — every replaced pack version leaves
-- its variants behind forever — so the lexical scan surface drifts away from the
-- live pack while the vector one stays pinned to it.
--
-- Position mirrors the other tables: valid_from, valid_to, is_active. Hence a
-- rebuild (ALTER ADD would append). qa_embedding references variant_id, so its FK
-- is dropped and restored around the rebuild; its rows are never touched.
--
-- is_active is BACKFILLED FROM THE PARENT ENTRY — the only honest source, since
-- until now activation existed nowhere else for a variant.
--
-- The three ts_* indexes and the trgm index become partial. NOTE: a partial index
-- is only usable when the query itself implies the predicate, so `e.is_active` on
-- the JOINED table is not enough — qa_exact and qa_lexical add `v.is_active` in
-- the same change. Without that the indexes would simply stop being used.

SET search_path TO edu_ai, public;

BEGIN;

CREATE TABLE qa_variants_bak149 AS SELECT * FROM qa_variants;

ALTER TABLE qa_embedding DROP CONSTRAINT qa_embedding_variant_id_fkey;
DROP TABLE qa_variants;

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
    -- validity
    valid_from         date,
    valid_to           date,
    is_active          boolean NOT NULL DEFAULT true,
    -- the settings generation that produced the reduced forms above
    setting_id         uuid REFERENCES settings(setting_id) ON UPDATE CASCADE,
    created_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT qa_variants_visibility_check
        CHECK (visibility = ANY (ARRAY['private','entity','organization']))
);

-- PARTIAL: the lexical scan surface is the LIVE pack, the same way
-- qa_embedding_hnsw_active pins the vector surface to it.
CREATE INDEX qa_variants_ts_norm_gin  ON qa_variants USING gin (ts_norm)  WHERE is_active;
CREATE INDEX qa_variants_ts_lemma_gin ON qa_variants USING gin (ts_lemma) WHERE is_active;
CREATE INDEX qa_variants_ts_stem_gin  ON qa_variants USING gin (ts_stem)  WHERE is_active;
CREATE INDEX qa_variants_norm_trgm    ON qa_variants USING gin (norm_text edu_ai.gin_trgm_ops) WHERE is_active;
CREATE INDEX qa_variants_active_idx   ON qa_variants (is_active) WHERE is_active;
CREATE INDEX qa_variants_entry_idx              ON qa_variants (entry_id);
CREATE INDEX qa_variants_setting_idx            ON qa_variants (setting_id);
CREATE INDEX qa_variants_legal_entity_idx       ON qa_variants (legal_entity_code);
CREATE INDEX qa_variants_entity_visibility_idx  ON qa_variants (legal_entity_code, visibility);
CREATE INDEX qa_variants_audience_roles_idx     ON qa_variants USING gin (audience_roles);
CREATE INDEX qa_variants_school_subject_ids_gin ON qa_variants USING gin (school_subject_ids);
CREATE INDEX qa_variants_domain_ids_gin         ON qa_variants USING gin (domain_ids);

COMMENT ON COLUMN qa_variants.is_active IS
    'Cascaded from qa_entries (migration 149). Present so the ts_*/trgm indexes can be PARTIAL — a replaced pack version must not stay in the lexical scan surface, exactly as qa_embedding_hnsw_active keeps it out of the vector one. qa_exact/qa_lexical must therefore carry `v.is_active` in the WHERE, or the partial indexes go unused.';
COMMENT ON COLUMN qa_variants.school_grade_ids IS
    'Copy of qa_entries.school_grade_ids. Denormalised because qa_exact/qa_lexical SCAN this table — see scope.py: a predicate that lived only on the joined entry would be applied after ranking.';
COMMENT ON COLUMN qa_variants.binding_id IS
    'taxonomy_binding of the pack, denormalised for parity with document_chunks (migration 144).';

-- ts_* are GENERATED: recomputed, never copied. is_active comes from the parent.
INSERT INTO qa_variants (variant_id, entry_id, processing_uuid, file_uuid, phrasing,
       norm_text, lemma_text, stem_text, school_grade_ids, school_level,
       school_subject_ids, domain_ids, binding_id, legal_entity_code, visibility,
       owner_user_id, audience_roles, valid_from, valid_to, is_active, setting_id, created_at)
OVERRIDING SYSTEM VALUE
SELECT b.variant_id, b.entry_id, b.processing_uuid, b.file_uuid, b.phrasing,
       b.norm_text, b.lemma_text, b.stem_text, b.school_grade_ids, b.school_level,
       b.school_subject_ids, b.domain_ids, b.binding_id, b.legal_entity_code, b.visibility,
       b.owner_user_id, b.audience_roles, b.valid_from, b.valid_to,
       COALESCE(e.is_active, true), b.setting_id, b.created_at
  FROM qa_variants_bak149 b
  LEFT JOIN qa_entries e ON e.entry_id = b.entry_id;

SELECT setval(pg_get_serial_sequence('qa_variants','variant_id'),
              GREATEST((SELECT COALESCE(MAX(variant_id), 0) FROM qa_variants), 1));

ALTER TABLE qa_embedding
    ADD CONSTRAINT qa_embedding_variant_id_fkey
    FOREIGN KEY (variant_id) REFERENCES qa_variants(variant_id) ON DELETE CASCADE;

DO $$
DECLARE a bigint; b bigint; mism bigint;
BEGIN
    SELECT (SELECT count(*) FROM qa_variants), (SELECT count(*) FROM qa_variants_bak149) INTO a, b;
    IF a <> b THEN RAISE EXCEPTION 'qa_variants: copied % of % rows', a, b; END IF;
    -- the whole point: a variant's activation must equal its entry's
    SELECT count(*) INTO mism FROM qa_variants v JOIN qa_entries e USING (entry_id)
     WHERE v.is_active <> e.is_active;
    IF mism > 0 THEN RAISE EXCEPTION '% variant(s) disagree with their entry on is_active', mism; END IF;
END $$;

DROP TABLE qa_variants_bak149;

COMMIT;
