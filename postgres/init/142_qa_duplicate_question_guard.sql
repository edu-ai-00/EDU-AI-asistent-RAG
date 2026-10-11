-- 142: report two active answers to the same question in one school (2026-08-18).
--
-- Pack replacement is BY NAME (Martin's decision, 2026-08-18): uploading pack
-- `prijimacky` replaces the previous `prijimacky` of that school and leaves
-- `skolni-rad` alone. That is the right granularity — a school keeps several
-- topical packs side by side — but it permits something the constraint cannot
-- catch: two DIFFERENT packs of the same school answering the SAME question.
--
-- Why that matters: qa_exact and qa_vector return LIMIT 1. With two active
-- answers the winner is arbitrary — not wrong-looking, just silently one of two.
-- Measured on live test data: three questions had two answers each across
-- cs-curated-zs and cs-bez-variant.
--
-- REPORTS, never blocks. A curator mid-reorganisation legitimately has both the
-- old and the new pack loaded for a moment; a hard constraint would refuse the
-- upload that is fixing the problem. Same principle as grade_levels_drift() and
-- qa_pack_drift().
--
-- Comparison folds the canonical question (lower + unaccent), so
-- "Jak se omlouvá absence?" and "jak se omlouva absence" count as one question.

CREATE OR REPLACE FUNCTION edu_ai.qa_duplicate_questions()
RETURNS TABLE (legal_entity_code text, question text, packs text, entries integer)
LANGUAGE sql STABLE AS $$
  SELECT e.legal_entity_code,
         min(e.canonical_q)              AS question,
         string_agg(DISTINCT e.pack_id, ' + ' ORDER BY e.pack_id) AS packs,
         count(*)::int                   AS entries
    FROM edu_ai.qa_entries e
   WHERE e.is_active
   GROUP BY e.legal_entity_code,
            lower(unaccent(e.canonical_q))
  HAVING count(*) > 1
$$;

COMMENT ON FUNCTION edu_ai.qa_duplicate_questions() IS
  'Questions answered twice within one school by different active packs. qa_exact/qa_vector return LIMIT 1, so a duplicate makes the winner arbitrary. Reports only — a curator swapping packs is briefly in this state on purpose.';
