-- last change in code by Martin Safranek (2026-07-22).
-- ─────────────────────────────────────────────────────────────────────────
-- chunk_exclusion_rule: user-defined rules to DROP chunks before insert.
--
-- Evaluated by the ChunkExclusionFilter component, which runs AFTER the
-- ChunkLemmatizer (so norm/lemma/stem columns exist) and BEFORE the vectorised
-- insert (so excluded chunks are never embedded).
--
-- Semantics:
--   * A chunk is EXCLUDED if it matches any ACTIVE rule.
--   * Within one rule, all NON-NULL conditions are ANDed
--     (scope + match + length/token). Across rules they are ORed.
--   * Rules are evaluated in `rule_order`; the FIRST matching rule excludes the
--     chunk (its rule_name is logged).
--   * `negate=true` inverts the rule's match result (keep-only / whitelist).
--   * Scope columns (source_type, txt_structure_type): NULL = applies to all.
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS edu_ai.chunk_exclusion_rule (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    rule_order    int         NOT NULL,                 -- evaluation order
    rule_name     text        NOT NULL,
    is_active     boolean     NOT NULL DEFAULT true,

    -- ── scope (NULL = all) ──
    source_type        text,                            -- 'web' | 'pdf' | 'office'
    txt_structure_type text,                            -- e.g. heading / table / paragraph

    -- ── match condition ──
    match_regex   text,                                 -- pattern (NULL = not checked)
    regex_target  text        DEFAULT 'chunk_text'
                  CHECK (regex_target IN ('chunk_text','chunk_norm_text',
                                          'chunk_lemma_text','chunk_stem_text')),
    match_mode    text        DEFAULT 'regex'
                  CHECK (match_mode IN ('regex','substring','exact')),
    negate        boolean     NOT NULL DEFAULT false,   -- invert match (keep-only)

    -- ── length / token conditions (NULL = not checked) ──
    min_chars          int,                             -- exclude if len(chunk_text) < min
    max_chars          int,                             -- exclude if len(chunk_text) > max
    min_content_tokens int,                             -- from chunk_norm_text (stopword-free)

    description   text,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS chunk_exclusion_rule_active
    ON edu_ai.chunk_exclusion_rule (rule_order) WHERE is_active;

-- source_type flows from the ingest API through the markdown flow onto each chunk.
ALTER TABLE edu_ai.edu_ai_document_chunks
    ADD COLUMN IF NOT EXISTS source_type text;
