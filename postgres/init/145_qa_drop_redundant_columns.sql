-- last change in code by Martin Safranek (2026-08-18).
-- 145: drop three redundant QA columns. One entity column, one truth about
-- ročník, one about předmět.
--
-- 1) owning_entity_code (all four QA tables) — added by migration 144 as
--    document parity, and that was a mistake. On a chunk the column is
--    informative because the LLM decides the chunk's level INDEPENDENTLY of the
--    uploading entity, so a document ingested under an umbrella org yields
--    chunks owned by its ZŠ unit (measured: 339 of 1298 chunks differ from
--    legal_entity_code). A QA pack takes its level FROM the uploading
--    institution, so the derived owner can only ever be that same institution
--    or NULL — redundant by construction, not by coincidence. Measured across
--    all four tables: 270 rows, 0 differ from legal_entity_code.
--    If the level ever stops coming from the institution, the column returns
--    together with that decision.
--
-- 2) qa_pack_manifest.subject / .grade — the scalar pair from produktový vlastník's
--    single-school era, superseded by school_subject_ids / school_grade_ids
--    (migration 140). Nothing reads them: route.py filtered on `grade = %s`
--    until 139 and today scopes packs purely by the id arrays. Keeping them
--    means two versions of the same fact with nothing keeping them in step,
--    and `subject` was NOT NULL, which forced the ingest to write a fake
--    'core' code that exists in no číselník.
--    Multi-value capability is the reason the arrays won: one pack legitimately
--    covers ročníky {5,7} or several subjects; a scalar cannot say that.
--
-- legal_entity_code stays as THE entity column on every QA table, and lang /
-- version / schema_version / thresholds on the manifest are untouched.

SET search_path TO edu_ai, public;

ALTER TABLE qa_embedding     DROP COLUMN IF EXISTS owning_entity_code;
ALTER TABLE qa_variants      DROP COLUMN IF EXISTS owning_entity_code;
ALTER TABLE qa_entries       DROP COLUMN IF EXISTS owning_entity_code;
ALTER TABLE qa_pack_manifest DROP COLUMN IF EXISTS owning_entity_code;

-- The unique index is (legal_entity_code, pack_id) — neither column takes part,
-- so nothing has to be rebuilt.
ALTER TABLE qa_pack_manifest DROP COLUMN IF EXISTS subject;
ALTER TABLE qa_pack_manifest DROP COLUMN IF EXISTS grade;

COMMENT ON COLUMN qa_pack_manifest.school_grade_ids IS
    'The pack''s ročníky as code_list ids of the owning entity — THE single truth (the scalar `grade` was dropped in 145). NULL/empty = all ročníky.';
COMMENT ON COLUMN qa_pack_manifest.school_subject_ids IS
    'The pack''s předměty as code_list ids — THE single truth (the scalar `subject` was dropped in 145). NULL = not subject-specific.';
