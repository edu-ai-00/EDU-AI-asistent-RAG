-- last change in code by Martin Safranek (2026-08-01).
-- 61_chunk_section_headings.sql
-- Each chunk carries the headings it lives under, in their own columns — never
-- merged into chunk_text.
--
-- The problem. A page element (one heading + its text) is split into several
-- chunks and only the FIRST holds the heading, so the rest are unreachable: of
-- "Seznam pomůcek na školní rok 2026/2027", chunks 1 and 2 are bare lists
-- ("4x 544, 2x 460, … vodovky, tempery") with coverage 0.000 for the query that
-- should find them. Measured: with the heading available to them, all three go
-- to coverage 1.000.
--
-- Why columns and not chunk_text. Copying the heading into the body was tried
-- (bullet_blocks.repeat_head) and it is what produced six ~6.3 kB near-copies of
-- one section, flooding ts_rank and burying the chunk that held the answer.
-- Separate columns give the same recall with none of the duplication — and they
-- can be WEIGHTED, which the body cannot: "pomůcka" appears in 21 chunk bodies
-- but in only 3 element headings, so a heading match is a 7x more selective
-- signal and must not be ranked as if it were prose.
--
-- Two levels, because documents have two kinds of heading:
--   section_heading      the element's own heading ("Seznam pomůcek … 2. stupeň")
--                        — answers "is this about supplies?"
--   subsection_heading   the last inner heading at or before this chunk
--                        ("**Třída – 7.**") — answers "which grade?"
--
-- The subsection is tracked WHILE splitting, and a heading applies FORWARD. That
-- also fixes a defect found on this same document: the splitter put
-- "**Třída – 7.**" at the END of chunk 0, detached from its list, so a query
-- about 7th grade matched a chunk containing the 6th-grade list. Attributing the
-- heading to the following chunk removes the mismatch without moving any text.
--
-- Reduced forms use the SAME eduai_normalize contract as the body columns, or
-- query↔index parity breaks. tsvectors here are UNWEIGHTED on purpose: the
-- weighting belongs to the query (document_lexical applies setweight), so it can
-- be tuned from the search config instead of being frozen into the schema.
--
-- Nullable throughout: a chunk whose element has no heading simply has none, and
-- rows written before this migration keep NULL until re-ingest.
--
-- Idempotent; transactional.

BEGIN;

ALTER TABLE edu_ai.edu_ai_document_chunks
    ADD COLUMN IF NOT EXISTS section_heading            text,
    ADD COLUMN IF NOT EXISTS section_heading_norm       text,
    ADD COLUMN IF NOT EXISTS section_heading_lemma      text,
    ADD COLUMN IF NOT EXISTS section_heading_stem       text,
    ADD COLUMN IF NOT EXISTS subsection_heading         text,
    ADD COLUMN IF NOT EXISTS subsection_heading_norm    text,
    ADD COLUMN IF NOT EXISTS subsection_heading_lemma   text,
    ADD COLUMN IF NOT EXISTS subsection_heading_stem    text;

COMMENT ON COLUMN edu_ai.edu_ai_document_chunks.section_heading IS
    'Heading of the element this chunk belongs to (same file_item_uuid). Never '
    'copied into chunk_text — see migration 61.';
COMMENT ON COLUMN edu_ai.edu_ai_document_chunks.subsection_heading IS
    'Last inner heading at or before this chunk. A heading applies forward, so a '
    'heading ending a chunk belongs to the next one — see migration 61.';

-- One tsvector per form over BOTH heading levels. `||` and coalesce are
-- IMMUTABLE, concat_ws is only STABLE — Postgres rejects the latter in a
-- generated column, which is why this is written the long way. Both heading
-- levels share one vector per form: a query never wants the section heading
-- without the subsection one, and one vector halves the index scans.
ALTER TABLE edu_ai.edu_ai_document_chunks
    ADD COLUMN IF NOT EXISTS ts_heading_norm tsvector
        GENERATED ALWAYS AS (to_tsvector('simple',
            coalesce(section_heading_norm, '') || ' ' ||
            coalesce(subsection_heading_norm, ''))) STORED,
    ADD COLUMN IF NOT EXISTS ts_heading_lemma tsvector
        GENERATED ALWAYS AS (to_tsvector('simple',
            coalesce(section_heading_lemma, '') || ' ' ||
            coalesce(subsection_heading_lemma, ''))) STORED,
    ADD COLUMN IF NOT EXISTS ts_heading_stem tsvector
        GENERATED ALWAYS AS (to_tsvector('simple',
            coalesce(section_heading_stem, '') || ' ' ||
            coalesce(subsection_heading_stem, ''))) STORED;

CREATE INDEX IF NOT EXISTS chunks_ts_heading_lemma_gin
    ON edu_ai.edu_ai_document_chunks USING gin (ts_heading_lemma);
CREATE INDEX IF NOT EXISTS chunks_ts_heading_stem_gin
    ON edu_ai.edu_ai_document_chunks USING gin (ts_heading_stem);

COMMIT;
