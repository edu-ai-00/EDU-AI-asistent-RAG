-- 152: qa_entries gets entry_key — a stable identity the curator controls (2026-08-19).
--
-- entry_id is minted fresh on every upload (`uuid.uuid4()` in QAVariantExploder),
-- so re-uploading the same pack gives the same content NEW ids. Three things
-- break on that, all silently:
--   * question_answer_log.matched_entry_id points at a deactivated row after the
--     next upload, so "how often was THIS curated answer served" has no answer
--     across versions — and that is the only signal telling which curated
--     answers earn their place;
--   * the mobile SQLite pack has entry_id as PRIMARY KEY, so every entry looks
--     new to the device and a delta sync cannot be built;
--   * PostgresInsertQAEmbedding already embeds incrementally
--     (WHERE q.variant_id IS NULL), but new entry_ids mint new variant_ids, so
--     everything counts as new. Fixing one typo in a 500-entry pack costs 500
--     embedding calls.
--
-- entry_key is the same decision already taken one level up for pack_id in
-- migrations 142/143: the name is the replacement key, it must be CHOSEN rather
-- than derived, and the curator has to see it. entry_id stays the internal key;
-- the insert will look it up by (legal_entity_code, pack_id, entry_key) and
-- REUSE it instead of minting.
--
-- Additive and inert on its own: nullable column plus a partial unique index.
-- Nothing reads it until the insert component is rewritten, and no reader can
-- notice it — there is no `SELECT *` against any QA table in search, the gateway,
-- the flows or tools (verified for migration 146, re-verified today). Retrieval
-- filters on is_active + scope and is untouched.
--
-- Applied while all four QA tables hold 0 rows on production, so there is no
-- backfill and no chance of a duplicate key. After the first curated pack this
-- migration would mean inventing keys for existing data; after the first pack a
-- device or the log points at, the identity could not be changed at all.
--
-- COLUMN ORDER, deliberately not fixed here. entry_key belongs to the identity
-- block by meaning, but ALTER appends, and rebuilding the table to place it
-- there (the way migration 146 did) would mean dropping and restoring
-- qa_variants_entry_id_fkey for a property the schema itself calls presentation
-- only. So it lands at the END, and init_clean mirrors it at the END too — the
-- invariant worth keeping is that a from-scratch container matches the migrated
-- database, not that this one column reads in the prettiest place. If these
-- tables are ever rebuilt again, move it up into the identity block then.

ALTER TABLE edu_ai.qa_entries ADD COLUMN IF NOT EXISTS entry_key text;

COMMENT ON COLUMN edu_ai.qa_entries.entry_key IS
  'Curator-controlled stable identity of the entry within its pack. Survives '
  'rephrasing the question, which entry_id cannot: entry_id is reassigned on '
  'every upload. Unique among ACTIVE rows per (legal_entity_code, pack_id); '
  'derived from a slug of canonical_q when a new CSV row leaves it empty.';

-- Partial, like every other "one active per ..." index here: history rows keep
-- their key, only the live set has to be unambiguous.
--
-- `entry_key IS NOT NULL` in the predicate is the load-bearing part, and it was
-- found by testing rather than by thinking. The first draft used NULLS NOT
-- DISTINCT (so two active rows could not both sit on an empty key) — which
-- INSTANTLY BREAKS the ingest that exists today: nothing populates entry_key
-- yet, so every entry arrives NULL and the second entry of any pack collides
-- with the first. Measured against production: `duplicate key ... Key
-- (legal_entity_code, pack_id, entry_key)=(…, exp_probe, null) already exists`
-- on a two-entry probe.
--
-- So the constraint applies only ONCE A KEY EXISTS. That is what makes this
-- migration genuinely inert: today's insert keeps working untouched, and the
-- uniqueness starts biting the moment the upsert begins writing keys. When that
-- lands, entry_key becomes NOT NULL and this predicate can go — a NULL key will
-- be impossible rather than merely unconstrained.
CREATE UNIQUE INDEX IF NOT EXISTS qa_entries_one_active_per_key
    ON edu_ai.qa_entries (legal_entity_code, pack_id, entry_key)
    WHERE is_active AND entry_key IS NOT NULL;

DO $$
DECLARE
  n_col int;
  n_idx int;
BEGIN
  SELECT count(*) INTO n_col FROM information_schema.columns
   WHERE table_schema = 'edu_ai' AND table_name = 'qa_entries'
     AND column_name = 'entry_key';
  SELECT count(*) INTO n_idx FROM pg_indexes
   WHERE schemaname = 'edu_ai' AND indexname = 'qa_entries_one_active_per_key';
  IF n_col <> 1 OR n_idx <> 1 THEN
    RAISE EXCEPTION 'mig 152: expected entry_key column and its index, got col=% idx=%',
                    n_col, n_idx;
  END IF;
END $$;
