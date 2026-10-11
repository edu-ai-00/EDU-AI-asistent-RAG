-- 159: the duplicate-question report covers what a CALLER sees, not one school
-- (2026-08-20).
--
-- Migration 142 reported two active answers to one question WITHIN one school.
-- That missed the case that actually reached a user: a school's pack and its
-- ORGANISATION's pack both answering "Jak se omlouvá absence?". Measured on
-- production with four packs loaded — 142's function returned nothing while a ZŠ
-- pupil was being served the organisation's generic answer instead of their own
-- school's, because qa_exact selected with LIMIT 1 and no ORDER BY.
--
-- qa_exact now orders by proximity (own school, then the organisation, then
-- anyone else), so that particular wrong answer is gone. The report still has to
-- exist, for two reasons:
--   * qa_vector has the same LIMIT 1 and has NOT been given the ordering yet;
--   * proximity cannot separate two packs of the SAME school, where the tie
--     falls back to entry_id — stable, but arbitrary. That is the ročník axis,
--     also not built yet.
-- So this reports the collisions those two gaps still decide by luck.
--
-- REPHRASED AROUND THE FAILURE. 142 grouped by legal_entity_code, which is a
-- fact about the table. The failure is "for THIS caller, this question has more
-- than one active answer and the winner is chosen for them", so the grouping is
-- now per VIEWER. `visibility = 'organization'` means everyone sharing the root
-- legal person (see organization_entities() in search/app/settings.py), so an
-- organisation's entry is visible to every unit under that root — which is
-- exactly how the collision arises.
--
-- The signature changes, hence DROP: CREATE OR REPLACE cannot alter a return
-- type. Nothing calls this function (checked across .py, .sql, .md and the flow
-- JSON — only three comments mention it), so no caller breaks.
--
-- REPORTS, never blocks — same principle as 142, grade_levels_drift() and
-- qa_pack_drift(). A curator mid-reorganisation is legitimately in this state.
--
-- KNOWN over-reporting, inherited from 142 and left deliberately: a `private`
-- entry is visible only to its owner, but is counted as visible to its entity.
-- Two private entries of DIFFERENT owners with the same question therefore show
-- as a collision nobody actually experiences. Adding the owner axis would make
-- the report exact; for a report, erring towards showing too much is the right
-- direction, and this is the same behaviour 142 already had.

DROP FUNCTION IF EXISTS edu_ai.qa_duplicate_questions();

CREATE FUNCTION edu_ai.qa_duplicate_questions()
RETURNS TABLE (seen_by text, kind text, question text,
               entities text, packs text, answers integer)
LANGUAGE sql STABLE AS $$
  WITH RECURSIVE up AS (
      -- every active entity, then walked UP towards its root legal person
      SELECT legal_entity_code AS ent, legal_entity_code AS cur, parent_code
        FROM edu_ai.legal_entity
       WHERE is_active
      UNION ALL
      SELECT u.ent, p.legal_entity_code, p.parent_code
        FROM up u
        JOIN edu_ai.legal_entity p
          ON p.legal_entity_code = u.parent_code AND p.is_active
  ),
  ent_root AS (
      SELECT ent, cur AS root FROM up WHERE parent_code IS NULL
  ),
  visible AS (
      -- an entry is always a candidate for its own entity
      SELECT e.legal_entity_code AS seen_by, e.entry_id, e.pack_id,
             e.canonical_q, e.legal_entity_code AS owner
        FROM edu_ai.qa_entries e
       WHERE e.is_active
      -- UNION, not UNION ALL: an organisation-visible entry of the viewer's own
      -- entity is produced by both branches and must count once, or every such
      -- entry would look like a duplicate of itself.
      UNION
      SELECT viewer.ent, e.entry_id, e.pack_id, e.canonical_q, e.legal_entity_code
        FROM edu_ai.qa_entries e
        JOIN ent_root owner  ON owner.ent = e.legal_entity_code
        JOIN ent_root viewer ON viewer.root = owner.root
       WHERE e.is_active AND e.visibility = 'organization'
  )
  SELECT v.seen_by,
         -- which gap decides this one: proximity already handles across_entities
         -- in qa_exact, so a remaining one_entity row is the ročník axis
         CASE WHEN count(DISTINCT v.owner) > 1 THEN 'across_entities'
              ELSE 'one_entity' END,
         min(v.canonical_q),
         string_agg(DISTINCT v.owner,   ' + ' ORDER BY v.owner),
         string_agg(DISTINCT v.pack_id, ' + ' ORDER BY v.pack_id),
         count(*)::int
    FROM visible v
   GROUP BY v.seen_by, lower(unaccent(v.canonical_q))
  HAVING count(*) > 1
$$;

COMMENT ON FUNCTION edu_ai.qa_duplicate_questions() IS
  'Questions with more than one active answer FOR ONE CALLER — their own entity''s entries plus everything visible to their organisation. qa_vector still selects LIMIT 1 without ordering, and two packs of one school tie on entry_id, so these are the answers still chosen by luck. Reports only; a curator swapping packs is briefly in this state on purpose.';

-- Say what the state IS, so the migration answers "how big is this" instead of
-- leaving it to be asked later.
DO $$
DECLARE
  n_all int;
  n_across int;
  n_one int;
BEGIN
  SELECT count(*) INTO n_all FROM edu_ai.qa_duplicate_questions();
  SELECT count(*) INTO n_across
    FROM edu_ai.qa_duplicate_questions() WHERE kind = 'across_entities';
  SELECT count(*) INTO n_one
    FROM edu_ai.qa_duplicate_questions() WHERE kind = 'one_entity';
  RAISE NOTICE '159: % colliding question(s) — % across entities, % within one entity',
               n_all, n_across, n_one;
END $$;
