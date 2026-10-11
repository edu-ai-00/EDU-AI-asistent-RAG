-- last change in code by Martin Safranek (2026-08-18).
-- 144: QA scan surfaces get the same metadata columns as a document chunk.
--
-- WHY. document_chunks is BOTH scan surfaces of the document side (lexical over
-- ts_*, vector over embedding), so every retrieval predicate and every ranking
-- signal sits on the scanned row. The QA side splits the same job over two
-- tables — qa_variants (lexical) and qa_embedding (vector) — and those two only
-- ever received the VISIBILITY set (entity/owner/roles/validity, migration 140).
-- Subject, domain, binding and the owning unit stayed on qa_entries, reachable
-- only through a JOIN.
--
-- For the VECTOR path that is a defect, not a preference: the HNSW scan orders
-- by distance and hands candidates outwards, so a predicate on the joined
-- qa_entries is applied only AFTER the index has already chosen who to offer.
-- With LIMIT 1 that silently narrows the candidate pool.
-- For the LEXICAL path the join is evaluated before the sort, so it is a
-- correctness no-op — there it buys consistency (qa_variants already carries
-- every other document-parity column) and lets a future ENTRY-level subject or
-- domain be filtered without a second join.
--
-- Naming follows the QA side where it already chose one (domain_ids, not
-- document_domain_id) and the document side where QA had nothing (binding_id,
-- owning_entity_code, embedding_dimension, embedding_provider).

SET search_path TO edu_ai, public;

-- ── declaration layer: the pack and its entries ──────────────────────────────
-- binding + owning unit had no QA home at all. They belong to the DECLARATION
-- first (a pack maps to one taxonomy_binding the same way a document does) and
-- are copied down to the scan surfaces from there.
ALTER TABLE qa_pack_manifest
    ADD COLUMN IF NOT EXISTS binding_id        bigint REFERENCES taxonomy_binding(id),
    ADD COLUMN IF NOT EXISTS owning_entity_code text  REFERENCES legal_entity(legal_entity_code);

ALTER TABLE qa_entries
    ADD COLUMN IF NOT EXISTS binding_id        bigint REFERENCES taxonomy_binding(id),
    ADD COLUMN IF NOT EXISTS owning_entity_code text  REFERENCES legal_entity(legal_entity_code);

-- ── lexical scan surface ─────────────────────────────────────────────────────
ALTER TABLE qa_variants
    ADD COLUMN IF NOT EXISTS school_subject_ids bigint[],
    ADD COLUMN IF NOT EXISTS domain_ids         bigint[],
    ADD COLUMN IF NOT EXISTS binding_id         bigint REFERENCES taxonomy_binding(id),
    ADD COLUMN IF NOT EXISTS owning_entity_code text  REFERENCES legal_entity(legal_entity_code);

-- ── vector scan surface ──────────────────────────────────────────────────────
-- embedding_dimension/provider are the two document_chunks columns qa_embedding
-- lacked: the model name alone cannot tell a 1536-d from a 3072-d vector when a
-- registry generation changes, and provider is what makes a mixed stack
-- auditable. They are NOT added to qa_variants — that row holds no vector; its
-- own generation is already pinned by setting_id.
ALTER TABLE qa_embedding
    ADD COLUMN IF NOT EXISTS school_subject_ids  bigint[],
    ADD COLUMN IF NOT EXISTS domain_ids          bigint[],
    ADD COLUMN IF NOT EXISTS binding_id          bigint REFERENCES taxonomy_binding(id),
    ADD COLUMN IF NOT EXISTS owning_entity_code  text  REFERENCES legal_entity(legal_entity_code),
    ADD COLUMN IF NOT EXISTS embedding_dimension integer,
    ADD COLUMN IF NOT EXISTS embedding_provider  text;

-- mirrors chunks_school_grade_ids_gin / chunks_school_subject_ids_gin: array
-- overlap (&&) is the access pattern on both sides.
CREATE INDEX IF NOT EXISTS qa_variants_school_subject_ids_gin  ON qa_variants  USING gin (school_subject_ids);
CREATE INDEX IF NOT EXISTS qa_variants_domain_ids_gin          ON qa_variants  USING gin (domain_ids);
CREATE INDEX IF NOT EXISTS qa_embedding_school_subject_ids_gin ON qa_embedding USING gin (school_subject_ids);
CREATE INDEX IF NOT EXISTS qa_embedding_domain_ids_gin         ON qa_embedding USING gin (domain_ids);

-- ── backfill: copy what qa_entries already knows onto both surfaces ───────────
-- binding_id/owning_entity_code stay NULL for rows ingested before this
-- migration — they were never derived, so there is nothing to copy. A re-ingest
-- of the pack fills them; that is the same rule the document side follows.
UPDATE qa_variants v
   SET school_subject_ids = e.school_subject_ids,
       domain_ids         = e.domain_ids
  FROM qa_entries e
 WHERE e.entry_id = v.entry_id
   AND (v.school_subject_ids IS DISTINCT FROM e.school_subject_ids
     OR v.domain_ids         IS DISTINCT FROM e.domain_ids);

UPDATE qa_embedding q
   SET school_subject_ids = e.school_subject_ids,
       domain_ids         = e.domain_ids
  FROM qa_entries e
 WHERE e.entry_id = q.entry_id
   AND (q.school_subject_ids IS DISTINCT FROM e.school_subject_ids
     OR q.domain_ids         IS DISTINCT FROM e.domain_ids);

-- embedding_dimension is recoverable from the stored vector itself, so the
-- backfill is exact rather than assumed; provider comes from the registry row
-- that produced the model.
UPDATE qa_embedding q
   SET embedding_dimension = vector_dims(q.embedding)
 WHERE q.embedding IS NOT NULL AND q.embedding_dimension IS NULL;

-- provider is not a column of embedding_registry (generation/model/dimension/
-- target_table only), so the backfill reads it off the document chunks embedded
-- with the SAME model — the one place in the DB that already recorded it. No
-- document chunk for that model => stays NULL and the next ingest fills it.
UPDATE qa_embedding q
   SET embedding_provider = p.embedding_provider
  FROM (SELECT DISTINCT embedding_model, embedding_provider
          FROM document_chunks
         WHERE embedding_provider IS NOT NULL) p
 WHERE p.embedding_model = q.embedding_model
   AND q.embedding_provider IS NULL;

COMMENT ON COLUMN qa_embedding.binding_id IS
    'taxonomy_binding of the pack, denormalised — the ANN scan filters here, not on qa_entries (migration 144).';
COMMENT ON COLUMN qa_variants.binding_id IS
    'taxonomy_binding of the pack, denormalised for parity with document_chunks (migration 144).';
