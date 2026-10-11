-- 138: grade→school-level mapping becomes DATA on the entity (2026-08-12, Martin).
-- (Renumbered from 136 — kolega pro infrastrukturu's 134/135 took those numbers on the shared branch.)
--
-- The rule "1..9 = ZŠ, 10..13 = SŠ" was hardcoded arithmetic in three places
-- (answer.py caller level, the MD insert components' school_level fallback,
-- server.py's range guard) — and a fourth time in the dropped trigger 131,
-- which counted 9999 as ZŠ where the insert component excludes it. Three
-- authors, three treatments of the same codebook code.
--
-- It is now one statement per school system, next to `units` (which already
-- describes the organisation's composition as jsonb on the entity). Shape
-- mirrors `units`: level as the key, its ročník codes as the value.
--
-- The column sits on legal_entity GENERALLY, not only on the top entity, and
-- resolves unit -> ancestors -> default. A group running a normal ZŠ AND a
-- víceleté gymnázium needs to say that ročník 7 is ZŠ in one unit and SŠ in
-- the other; only a per-unit override can express that. NULL on a unit (the
-- normal case) inherits the organisation's map.
--
-- Why this beats the arithmetic it replaces: an eight-year gymnasium is ONE
-- entity with level='SS' whose prima..kvarta are ročníky 6..9. The arithmetic
-- calls a kvartán "ZŠ", so answer.py's level focus then drops every SŠ chunk
-- — the school's own school rules. With a map the school just declares
-- {"SS": ["6",...,"13"]}.

ALTER TABLE edu_ai.legal_entity ADD COLUMN IF NOT EXISTS grade_levels jsonb;

COMMENT ON COLUMN edu_ai.legal_entity.grade_levels IS
  'Which ročník codes belong to which school level, e.g. {"ZS":["1".."9"],"SS":["10".."13"]}. Read ONLY through edu_ai.grade_level(); resolves unit -> ancestors, NULL everywhere = the Czech default in that function. Keys must be legal_entity.level values (MS/ZS/SS), values must be code_list(school_grade) codes of this entity''s resolved codebook — jsonb cannot FK, so edu_ai.grade_levels_drift() is the guard.';

UPDATE edu_ai.legal_entity SET grade_levels =
  '{"ZS": ["1","2","3","4","5","6","7","8","9"], "SS": ["10","11","12","13"]}'::jsonb
 WHERE legal_entity_code = 'SKOLA_NOVA_VES';

UPDATE edu_ai.legal_entity SET grade_levels =
  '{"ZS": ["1","2","3","4","5","6","7","8","9"]}'::jsonb
 WHERE legal_entity_code IN ('ZAKLADNI_SKOLA_LIPOVA', 'ZAKLADNI_SKOLA_BOROVA');
-- Units inherit: SKOLA_NOVA_VES_ZS / _SOS stay NULL on purpose — their parent's
-- map already answers for both, and a per-unit copy could drift from it.


-- The ONE place the mapping is read. Callers never touch grade_levels.
CREATE OR REPLACE FUNCTION edu_ai.grade_level(p_entity text, p_code text)
RETURNS text
LANGUAGE plpgsql STABLE AS $$
DECLARE
  v_map jsonb;
  v_level text;
  v_n int;
BEGIN
  IF p_code IS NULL OR btrim(p_code) = '' THEN
    RETURN NULL;
  END IF;

  -- nearest map walking UP the entity chain (unit -> its legal person -> …).
  -- depth bound: parent_code has no cycle constraint, and a stack overflow
  -- inside a search node would surface as "no level" at best.
  WITH RECURSIVE chain AS (
    SELECT legal_entity_code, parent_code, grade_levels, 0 AS depth
      FROM edu_ai.legal_entity WHERE legal_entity_code = p_entity
    UNION ALL
    SELECT e.legal_entity_code, e.parent_code, e.grade_levels, c.depth + 1
      FROM edu_ai.legal_entity e JOIN chain c ON e.legal_entity_code = c.parent_code
     WHERE c.depth < 10
  )
  SELECT grade_levels INTO v_map
    FROM chain WHERE grade_levels IS NOT NULL ORDER BY depth LIMIT 1;

  IF v_map IS NOT NULL THEN
    -- a map present is AUTHORITATIVE: a code it does not list has no level
    -- (9999 "all grades" and UNKNOWN are exactly that, deliberately)
    SELECT k INTO v_level
      FROM jsonb_each(v_map) AS t(k, v)
     WHERE v ? p_code
     LIMIT 1;
    RETURN v_level;
  END IF;

  -- No map anywhere: the SINGLE remaining copy of the old arithmetic. Kept so
  -- a tenant that has not declared its system yet degrades to the Czech default
  -- instead of losing grade/level preference altogether; grade_levels_drift()
  -- reports who is running on it.
  BEGIN
    v_n := p_code::int;
  EXCEPTION WHEN others THEN
    RETURN NULL;
  END;
  IF v_n BETWEEN 1 AND 9 THEN
    RETURN 'ZS';
  ELSIF v_n BETWEEN 10 AND 13 THEN
    RETURN 'SS';
  END IF;
  RETURN NULL;
END $$;

COMMENT ON FUNCTION edu_ai.grade_level(text, text) IS
  'The school level of a ročník code for an entity: nearest grade_levels map walking up the entity chain, else the Czech 1..9/10..13 default. The only reader of legal_entity.grade_levels.';


-- jsonb cannot reference the codebook, so a wrong value does not raise — it
-- silently relabels. This reports both directions of drift; it belongs in the
-- seam-check suite, not in a trigger, because a legitimate mid-migration state
-- (codebook extended, map not yet) must not block the write.
CREATE OR REPLACE FUNCTION edu_ai.grade_levels_drift()
RETURNS TABLE (legal_entity_code text, problem text, detail text)
LANGUAGE sql STABLE AS $$
  -- 1. map lists a code NOBODY in the entity's subtree has in its codebook.
  -- The subtree, not the entity itself: an umbrella organisation's own codebook
  -- holds just 9999 (SKOLA_NOVA_VES) while the numeric ročníky live on the units
  -- that inherit its map — checking only the entity would flag every one of them.
  SELECT e.legal_entity_code, 'code not in codebook', string_agg(DISTINCT c.code, ',')
    FROM edu_ai.legal_entity e
    CROSS JOIN LATERAL jsonb_each(e.grade_levels) AS m(k, v)
    CROSS JOIN LATERAL jsonb_array_elements_text(m.v) AS c(code)
   WHERE e.grade_levels IS NOT NULL
     AND NOT EXISTS (
       SELECT 1
         FROM edu_ai.legal_entity d
         CROSS JOIN LATERAL edu_ai.resolved_code_list(d.legal_entity_code, 'school_grade') r
        WHERE (d.legal_entity_code = e.legal_entity_code
               OR d.parent_code = e.legal_entity_code)
          AND r.code = c.code AND r.is_active)
   GROUP BY 1
  UNION ALL
  -- 2. map key is not a level the model allows
  SELECT e.legal_entity_code, 'unknown level key', string_agg(DISTINCT m.k, ',')
    FROM edu_ai.legal_entity e CROSS JOIN LATERAL jsonb_each(e.grade_levels) AS m(k, v)
   WHERE e.grade_levels IS NOT NULL AND m.k NOT IN ('MS', 'ZS', 'SS')
   GROUP BY 1
  UNION ALL
  -- 3. active numeric ročník nobody classified — the entity runs on the default
  SELECT e.legal_entity_code, 'grade not classified', string_agg(DISTINCT r.code, ',')
    FROM edu_ai.legal_entity e
    CROSS JOIN LATERAL edu_ai.resolved_code_list(e.legal_entity_code, 'school_grade') r
   WHERE e.is_active AND r.is_active AND r.code ~ '^[0-9]{1,2}$'
     AND edu_ai.grade_level(e.legal_entity_code, r.code) IS NULL
   GROUP BY 1
$$;
