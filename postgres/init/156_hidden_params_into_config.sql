-- last change in code by Martin Safranek (2026-08-20).
--
-- 156 — the tunables the code read but no settings row carried become visible.
--
-- Each value below is EXACTLY the code's own default, so behaviour does not
-- change; what changes is that the knob can be seen and turned. Merge is
-- `existing || new`, so a value already tuned on a school row survives.
--
-- Why bother with knobs that look inert today: ordering is TIERED
-- (answer.py::_sortkey → (sib, grade, priority, [heading,] authority, -fused,
-- -score)). A parameter inside `fused` decides nothing while an earlier tier
-- separates the chunks — and decides everything once it does not. Measured on
-- "Co se hodnotí u přijímacích zkoušek?": with priority_tier ON, bm25_weight 0
-- vs 5 changed nothing; with priority_tier OFF it went 3 sources → 1. So
-- "no effect today" is a property of the current configuration, not of the
-- parameter, and leaving such a knob out of the settings is what makes the next
-- person unable to fix the ordering they just changed.
--
-- NOT added, with reasons (so this is not re-litigated):
--   clarify_max_match_score, related_suggestions_top_n, log_candidates_n
--       — the code has no default; a row would mean inventing a value.
--   rerank, rerank_backend
--       — documented back-compat aliases for rerank_mode, which is already here.
--   enabled (inside a step's params), skip_siblings
--       — the executor already honours the step-level `enabled`; a second switch
--         in the same place would be a trap. skip_siblings is a runtime escape
--         hatch, not configuration.
--   llm_variants
--       — a nested sub-config, not a scalar knob.
--   max_turns, context.enabled
--       — already in config.context.
--   t_answer, t_suggest, m_margin
--       — already data, in qa_pack_manifest (per pack); the step param is only an
--         experiment override.

BEGIN;

CREATE TEMP TABLE _len_before ON COMMIT DROP AS
SELECT setting_id, jsonb_array_length(config::jsonb -> 'pipeline') AS n
  FROM edu_ai.settings
 WHERE is_active AND jsonb_typeof(config::jsonb -> 'pipeline') = 'array';

WITH new_params(step, params) AS (
    VALUES
      ('answer', jsonb_build_object(
          'bm25_weight', 0.03,               -- BM25 share of the fused score
          'heading_match_boost', 0.12,       -- chunk whose HEADING is the query
          'vector_element_weight', 1.0,      -- weight of a vector hit
          'keyword_weight', 1.0,             -- weight of a keyword hit
          'source_min_ratio', 0.5,           -- when a 2nd document earns a citation
          'priority_tier', true,             -- source authority as an ordering tier
          'domain_match', true,              -- same-domain chunk goes first
          'domain_kw_max_df_share', 0.25,    -- too-common words dropped from domain match
          'binding_fallback', true,          -- domain x level -> grade fallback rule
          'focus_gate', true,                -- 2nd source must share a concept
          'focus_max_df', 0.05,              -- what counts as a rare concept there
          'focus_require', 'all',            -- all shared concepts, or any one
          'rerank_top_k', 20)),              -- how many go to the cross-encoder
      ('document_lexical', jsonb_build_object(
          'trgm_fallback', true,             -- typo net when the tsquery finds nothing
          'trgm_min_sim', 0.35,              -- per-token similarity floor
          'trgm_min_typo_coverage', 0.5,     -- share of query tokens that must pass
          'trgm_fetch_k', 15,                -- typo candidates pulled
          'bm25_lexical', true,              -- BM25 instead of ts_rank
          'bm25_min_ratio', 0.65,            -- floor relative to the round's best
          'bm25_max_df_share', 0.25,         -- df gate on BM25 query terms
          'idf_coverage', true)),            -- rare concept decides coverage
      ('rag_keywords', jsonb_build_object(
          'topical_max_df_share', 0.25)),    -- what counts as too generic a concept
      ('rag_vector', jsonb_build_object(
          'grade_query_enrich', true,        -- put the ročník into the embedded query
          'grade_query_prefix', 'Pro {grade}. ročník: ')),
      ('document_siblings', jsonb_build_object(
          'grade_recall', 'from_pool_docs',  -- pull grade-tagged chunks from pool docs
          'grade_recall_max', 8))
)
UPDATE edu_ai.settings s
   SET config = jsonb_set(s.config::jsonb, '{pipeline}', (
         SELECT jsonb_agg(
                  CASE WHEN np.params IS NULL THEN e.elem
                       ELSE jsonb_set(e.elem, '{params}',
                              CASE WHEN jsonb_typeof(e.elem -> 'params') = 'object'
                                   THEN e.elem -> 'params' ELSE '{}'::jsonb END
                              || np.params)
                  END ORDER BY e.ord)
           FROM jsonb_array_elements(s.config::jsonb -> 'pipeline')
                WITH ORDINALITY AS e(elem, ord)
           LEFT JOIN new_params np ON np.step = e.elem ->> 'step'
       ))::json
 WHERE s.is_active
   AND jsonb_typeof(s.config::jsonb -> 'pipeline') = 'array'
   AND jsonb_array_length(s.config::jsonb -> 'pipeline') > 0;

DO $$
DECLARE
    r record;
    n int;
BEGIN
    FOR r IN
        SELECT * FROM (VALUES
          ('answer', array['bm25_weight','heading_match_boost','vector_element_weight',
                           'keyword_weight','source_min_ratio','priority_tier',
                           'domain_match','domain_kw_max_df_share','binding_fallback',
                           'focus_gate','focus_max_df','focus_require','rerank_top_k']),
          ('document_lexical', array['trgm_fallback','trgm_min_sim',
                           'trgm_min_typo_coverage','trgm_fetch_k','bm25_lexical',
                           'bm25_min_ratio','bm25_max_df_share','idf_coverage']),
          ('rag_keywords', array['topical_max_df_share']),
          ('rag_vector', array['grade_query_enrich','grade_query_prefix']),
          ('document_siblings', array['grade_recall','grade_recall_max'])
        ) AS t(step, keys)
    LOOP
        SELECT count(*) INTO n
          FROM edu_ai.settings s, jsonb_array_elements(s.config::jsonb->'pipeline') e
         WHERE s.is_active AND e->>'step' = r.step
           AND NOT (e -> 'params' ?& r.keys);
        IF n > 0 THEN
            RAISE EXCEPTION '156: % keys missing on % row(s) for step %',
                            array_length(r.keys,1), n, r.step;
        END IF;
    END LOOP;

    SELECT count(*) INTO n
      FROM _len_before b JOIN edu_ai.settings s USING (setting_id)
     WHERE s.config IS NULL
        OR jsonb_array_length(s.config::jsonb -> 'pipeline') <> b.n;
    IF n > 0 THEN
        RAISE EXCEPTION '156: pipeline length changed on % row(s)', n;
    END IF;

    RAISE NOTICE '156: 26 parameters now visible on every active settings row';
END $$;

COMMIT;
