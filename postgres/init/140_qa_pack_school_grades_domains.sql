-- 140: the QA pack gets the document model — school scope, multi-grade,
-- school level, subject, domain and validity (2026-08-15, Martin).
--
-- The QA side was designed (produktový vlastník's qa_pack_sqlite_vs_postgres.md) when there
-- was one school and one grade: `grade integer` on the manifest, nothing on the
-- rows. Documents grew a richer model since — ročníky as a SET of entity-resolved
-- ids, ZŠ/SŠ level, per-unit ownership — and the two drifted apart. This closes
-- the gap so the machinery already written for chunks (grade focus, level focus,
-- domain priority, validity) can apply to curated answers too.
--
-- WHY ids and not numbers: a ročník is a ROW of THAT UNIT's číselník. SOŠ owns
-- codes 10..13, a ZŠ owns 1..9, both plus 9999. Storing ids (like
-- document_metadata.school_grade_user) means "SŠ = 10,11,12,13" comes from the
-- database per entity and survives a codebook change.
--
-- WHY the same columns on four tables: scope.py and qa_vector.py both state the
-- rule — a filter must sit on the table being SCANNED. qa_exact/qa_lexical scan
-- qa_variants, qa_vector scans qa_embedding and orders by `embedding <=> query`
-- BEFORE joining qa_entries, so a predicate on the joined row is applied after
-- the index already offered rows the caller may not see. That is exactly why
-- legal_entity_code/visibility/owner_user_id/audience_roles are ALREADY
-- denormalised onto all three. Ročník and level are hard filters on queries that
-- return LIMIT 1 — one wrong row is a wrong ANSWER, not a worse ranking — so they
-- follow the same path. The single writer is the pack build; no triggers.
--
-- Subject and domain stop at the pack and the entry: they classify and (domain)
-- feed the fusion priority, they are not scan predicates. If that changes they
-- can be denormalised the same way.
--
-- owning_entity_code is deliberately NOT added: for a chunk it is needed because
-- an ORG ingests a document and the chunk is only then resolved to a unit. A
-- pack's owning unit IS its legal_entity_code.

-- ── 1) declaration level ─────────────────────────────────────────────────────
ALTER TABLE edu_ai.qa_pack_manifest
    ADD COLUMN IF NOT EXISTS school_grade_ids   bigint[],
    ADD COLUMN IF NOT EXISTS school_level       text,
    ADD COLUMN IF NOT EXISTS school_subject_ids bigint[],
    ADD COLUMN IF NOT EXISTS domain_ids         bigint[],
    ADD COLUMN IF NOT EXISTS audience_roles     text[],
    ADD COLUMN IF NOT EXISTS valid_from         date,
    ADD COLUMN IF NOT EXISTS valid_to           date;

DO $$ BEGIN
    ALTER TABLE edu_ai.qa_pack_manifest
        ADD CONSTRAINT qa_pack_manifest_school_level_check
        CHECK (school_level IS NULL OR school_level = ANY (ARRAY['MS','ZS','SS']));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

COMMENT ON COLUMN edu_ai.qa_pack_manifest.school_grade_ids IS
  'Ročníky this pack is for: code_list(school_grade) ROW IDS from the pack entity''s reachable codebook (SOŠ 10..13, ZŠ 1..9, 9999 = all). NULL/empty = every ročník. Mirrors document_metadata.school_grade_user.';
COMMENT ON COLUMN edu_ai.qa_pack_manifest.school_level IS
  'MS | ZS | SS — the school level this pack speaks to, DECLARED (Martin 2026-08-15) rather than only derived, so a pack with no ročníky can still be level-scoped. It must agree with what edu_ai.grade_level() derives from school_grade_ids; qa_pack_drift() reports a disagreement, because two sources of the same truth can part ways.';
COMMENT ON COLUMN edu_ai.qa_pack_manifest.school_subject_ids IS
  'code_list(school_subject) ids the pack covers. Supersedes the free-text `subject`, which stays only as the human part of pack_id.';
COMMENT ON COLUMN edu_ai.qa_pack_manifest.domain_ids IS
  'Declared taxonomy_rules DOMAIN ids, resolved against the entity''s own catalog (edu_ai.resolved_taxonomy_rules). Mirrors document_metadata.document_domain_user.';
COMMENT ON COLUMN edu_ai.qa_pack_manifest.audience_roles IS
  'code_list(role) codes the pack is addressed to. Same hard-filter semantics as everywhere else; NULL/empty = no restriction at pack level, per-entry roles still apply.';
COMMENT ON COLUMN edu_ai.qa_pack_manifest.grade IS
  'DEPRECATED by migration 140 — superseded by school_grade_ids (a SET of entity-resolved ids). Kept one cycle so a reader still selecting it does not break.';

-- ── 2) answer level ──────────────────────────────────────────────────────────
ALTER TABLE edu_ai.qa_entries
    ADD COLUMN IF NOT EXISTS school_grade_ids   bigint[],
    ADD COLUMN IF NOT EXISTS school_level       text,
    ADD COLUMN IF NOT EXISTS school_subject_ids bigint[],
    ADD COLUMN IF NOT EXISTS domain_ids         bigint[],
    ADD COLUMN IF NOT EXISTS valid_from         date,
    ADD COLUMN IF NOT EXISTS valid_to           date;

COMMENT ON COLUMN edu_ai.qa_entries.school_grade_ids IS
  'EFFECTIVE ročníky of this answer. Inherited from the pack at build time; a curator may override per entry. Same relationship as document_metadata.school_grade_user -> document_chunks.school_grade_ids (migration 138): the declaration fills in only what the row does not state itself.';

-- ── 3) scan surfaces ─────────────────────────────────────────────────────────
-- Only what a query filters on. Copies, written by the pack build.
ALTER TABLE edu_ai.qa_variants
    ADD COLUMN IF NOT EXISTS school_grade_ids bigint[],
    ADD COLUMN IF NOT EXISTS school_level     text,
    ADD COLUMN IF NOT EXISTS valid_from       date,
    ADD COLUMN IF NOT EXISTS valid_to         date;

ALTER TABLE edu_ai.qa_embedding
    ADD COLUMN IF NOT EXISTS school_grade_ids bigint[],
    ADD COLUMN IF NOT EXISTS school_level     text,
    ADD COLUMN IF NOT EXISTS valid_from       date,
    ADD COLUMN IF NOT EXISTS valid_to         date;

COMMENT ON COLUMN edu_ai.qa_variants.school_grade_ids IS
  'Copy of qa_entries.school_grade_ids. Denormalised because qa_exact/qa_lexical SCAN this table — see scope.py: a predicate that lived only on the joined entry would be applied after ranking.';
COMMENT ON COLUMN edu_ai.qa_embedding.school_grade_ids IS
  'Copy of qa_entries.school_grade_ids. Denormalised because the nearest-neighbour scan orders by `embedding <=> query` and joins per candidate — the same reason legal_entity_code/visibility already live here.';

-- ── 4) which grade rows a pack entity may reference ──────────────────────────
-- The entity's SUBTREE (itself + its units) UNION what resolves for the entity
-- (self -> ancestors -> global). The subtree half is not optional:
-- resolved_code_list walks UP only, so an umbrella organisation resolves to
-- {9999} alone and could never carry a ročník, even though its ZŠ owns 1..9 and
-- its SOŠ 10..13. The insert component does exactly this for chunks.
-- A FUNCTION, not a view: the QA build has to resolve ročník codes to ids
-- BEFORE the pack row exists, so anything derived from qa_pack_manifest would be
-- a chicken-and-egg. Parameterised, it serves the writer and the drift check
-- alike. Named generically because nothing about it is QA-specific.
DROP VIEW IF EXISTS edu_ai.qa_pack_allowed_grade;

CREATE OR REPLACE FUNCTION edu_ai.entity_grade_scope(p_entity text)
RETURNS TABLE (id bigint, code text)
LANGUAGE sql STABLE AS $$
  WITH RECURSIVE tree AS (
      SELECT p_entity AS node
      UNION ALL
      SELECT e.legal_entity_code
        FROM edu_ai.legal_entity e JOIN tree t ON e.parent_code = t.node
       WHERE e.is_active
  )
  SELECT c.id, c.code
    FROM tree t
    JOIN edu_ai.code_list c ON c.legal_entity_code = t.node
     AND c.list_code = 'school_grade' AND c.is_active
  UNION
  SELECT r.id, r.code
    FROM edu_ai.resolved_code_list(p_entity, 'school_grade') r
   WHERE r.is_active
$$;

COMMENT ON FUNCTION edu_ai.entity_grade_scope(text) IS
  'Ročník rows an entity may reference: its SUBTREE (itself + units) UNION resolved_code_list (self/ancestors/global). The subtree half is required because the resolver walks UP only — an umbrella org resolves to {9999} alone although its ZŠ owns 1..9 and its SOŠ 10..13. Used by the QA build (before the pack row exists) and by qa_pack_drift().';

-- ── 5) backfill the deprecated scalar ────────────────────────────────────────
UPDATE edu_ai.qa_pack_manifest m
   SET school_grade_ids = ARRAY[(
         SELECT a.id FROM edu_ai.entity_grade_scope(m.legal_entity_code) a
          WHERE a.code = m.grade::text
          LIMIT 1)]
 WHERE m.grade IS NOT NULL
   AND m.school_grade_ids IS NULL
   AND EXISTS (SELECT 1 FROM edu_ai.entity_grade_scope(m.legal_entity_code) a
                WHERE a.code = m.grade::text);

-- ── 6) drift guard ───────────────────────────────────────────────────────────
-- Arrays cannot carry a foreign key and a denormalised copy cannot be enforced,
-- so a wrong value does not raise — it silently mislabels an answer. A query,
-- not a trigger: a legitimate mid-build state must not block a write.
-- Same pattern as edu_ai.grade_levels_drift() (138). Belongs in the seam suite.
CREATE OR REPLACE FUNCTION edu_ai.qa_pack_drift()
RETURNS TABLE (pack_id text, legal_entity_code text, problem text, detail text)
LANGUAGE sql STABLE AS $$
  -- 1. ročník id the pack entity may not reference
  SELECT m.pack_id, m.legal_entity_code, 'grade id not in entity codebook',
         string_agg(DISTINCT g::text, ',')
    FROM edu_ai.qa_pack_manifest m
    CROSS JOIN LATERAL unnest(m.school_grade_ids) AS g
   WHERE m.is_active AND m.school_grade_ids IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM edu_ai.entity_grade_scope(m.legal_entity_code) a
                      WHERE a.id = g)
   GROUP BY 1, 2
  UNION ALL
  -- 2. domain id outside the entity's own taxonomy catalog
  SELECT m.pack_id, m.legal_entity_code, 'domain id not in entity catalog',
         string_agg(DISTINCT d::text, ',')
    FROM edu_ai.qa_pack_manifest m
    CROSS JOIN LATERAL unnest(m.domain_ids) AS d
   WHERE m.is_active AND m.domain_ids IS NOT NULL
     AND NOT EXISTS (SELECT 1
                       FROM edu_ai.resolved_taxonomy_rules(m.legal_entity_code, 'DOMAIN') t
                      WHERE t.id = d AND t.is_active)
   GROUP BY 1, 2
  UNION ALL
  -- 3. subject id outside the entity's own číselník
  SELECT m.pack_id, m.legal_entity_code, 'subject id not in entity codebook',
         string_agg(DISTINCT s::text, ',')
    FROM edu_ai.qa_pack_manifest m
    CROSS JOIN LATERAL unnest(m.school_subject_ids) AS s
   WHERE m.is_active AND m.school_subject_ids IS NOT NULL
     AND NOT EXISTS (SELECT 1
                       FROM edu_ai.resolved_code_list(m.legal_entity_code, 'school_subject') r
                      WHERE r.id = s AND r.is_active)
   GROUP BY 1, 2
  UNION ALL
  -- 4. role that is not a code_list('role') code
  SELECT m.pack_id, m.legal_entity_code, 'unknown audience role',
         string_agg(DISTINCT r, ',')
    FROM edu_ai.qa_pack_manifest m
    CROSS JOIN LATERAL unnest(m.audience_roles) AS r
   WHERE m.is_active AND m.audience_roles IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM edu_ai.code_list c
                      WHERE c.list_code = 'role' AND c.code = r AND c.is_active)
   GROUP BY 1, 2
  UNION ALL
  -- 5. declared school_level contradicts what the ročníky derive — the price of
  -- storing the level explicitly instead of only deriving it
  SELECT m.pack_id, m.legal_entity_code, 'declared level vs grades',
         'declared=' || m.school_level || ' derived=' || string_agg(DISTINCT l.lv, ',')
    FROM edu_ai.qa_pack_manifest m
    CROSS JOIN LATERAL unnest(m.school_grade_ids) AS g
    CROSS JOIN LATERAL (
      SELECT edu_ai.grade_level(m.legal_entity_code, a.code) AS lv
        FROM edu_ai.entity_grade_scope(m.legal_entity_code) a
       WHERE a.id = g
       LIMIT 1) AS l
   WHERE m.is_active AND m.school_level IS NOT NULL AND l.lv IS NOT NULL
   GROUP BY 1, 2, m.school_level
  HAVING bool_or(l.lv <> m.school_level)
  UNION ALL
  -- 6. ročníky spanning both levels — legal, but a pack that is "for ZŠ and SŠ
  -- at once" cannot be filtered sensibly
  SELECT m.pack_id, m.legal_entity_code, 'pack spans ZS and SS',
         string_agg(DISTINCT l.lv, ',')
    FROM edu_ai.qa_pack_manifest m
    CROSS JOIN LATERAL unnest(m.school_grade_ids) AS g
    CROSS JOIN LATERAL (
      SELECT edu_ai.grade_level(m.legal_entity_code, a.code) AS lv
        FROM edu_ai.entity_grade_scope(m.legal_entity_code) a
       WHERE a.id = g
       LIMIT 1) AS l
   WHERE m.is_active AND l.lv IS NOT NULL
   GROUP BY 1, 2
  HAVING count(DISTINCT l.lv) > 1
  UNION ALL
  -- 7. a scan-surface copy that disagrees with its entry (the denormalisation
  -- this migration introduces is only safe while the copies match)
  SELECT e.pack_id, e.legal_entity_code, 'variant copy differs from entry',
         string_agg(DISTINCT v.variant_id::text, ',')
    FROM edu_ai.qa_variants v
    JOIN edu_ai.qa_entries e ON e.entry_id = v.entry_id
   WHERE e.is_active
     AND (v.school_grade_ids IS DISTINCT FROM e.school_grade_ids
          OR v.school_level  IS DISTINCT FROM e.school_level)
   GROUP BY 1, 2
  UNION ALL
  SELECT e.pack_id, e.legal_entity_code, 'embedding copy differs from entry',
         string_agg(DISTINCT q.variant_id::text, ',')
    FROM edu_ai.qa_embedding q
    JOIN edu_ai.qa_entries e ON e.entry_id = q.entry_id
   WHERE e.is_active AND q.is_active
     AND (q.school_grade_ids IS DISTINCT FROM e.school_grade_ids
          OR q.school_level  IS DISTINCT FROM e.school_level)
   GROUP BY 1, 2
$$;

COMMENT ON FUNCTION edu_ai.qa_pack_drift() IS
  'Reports QA packs whose array ids do not resolve in their entity (grades, domains, subjects, roles), whose declared school_level contradicts its ročníky, that mix ZŠ and SŠ, or whose denormalised scan copies disagree with the entry. Arrays cannot FK and copies cannot be constrained; this is the guard. Mirror of edu_ai.grade_levels_drift().';


-- STILL TO DO OUTSIDE THIS MIGRATION:
--   * route._scope(): pick packs through scope.where() (entity + visibility +
--     role + validity) and match ročník by ARRAY OVERLAP, not `grade = %s`.
--   * qa_exact / qa_lexical / qa_vector: now that valid_from/valid_to exist on
--     the scanned tables, pass validity=True — until 139 the columns were simply
--     absent, so the call could not have worked.
--   * pack_id derivation (gateway): language-subject-grade cannot express a
--     multi-grade pack; move the ročník out of the identity.
