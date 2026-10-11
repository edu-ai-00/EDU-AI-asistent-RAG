-- last change in code by Martin Safranek (2026-08-01).
-- 45_chunking_config.sql
-- Move the chunking algorithm's thresholds out of the component code and into the
-- versioned config, so chunking becomes data like everything else (retrieval,
-- prompts, stopwords, taxonomy). Two components are covered:
--
--   split_text_into_chunks   — 10 thresholds were hardcoded; only chunk_size,
--                              chunk_overlap and min_chunk_chars were reachable.
--   validate_text_chunks     — 7 thresholds were hardcoded; only min_word_count,
--                              min_lang_score and langdetect_ignore_word_count
--                              were reachable.
--
-- The requirement that started this: turning OFF the repetition of a bullet
-- block's heading on every chunk (bullet_blocks.repeat_head), and controlling the
-- character limit that decides what counts as that heading
-- (bullet_blocks.head_max_chars).
--
-- RESOLUTION ORDER in the components (three levels):
--     a FILLED component parameter  ->  this config  ->  the code default
-- The component parameters exist for testing a single run; they are left EMPTY in
-- the flow JSON so that this config is what production reads. The code default is
-- reached only when the database is unreachable (same graceful pattern as
-- _fetch_prompt_from_db, which returns None and lets the ingest continue).
--
-- Every value below is COPIED FROM THE CURRENT EFFECTIVE STATE (flow JSON values
-- where a parameter existed, code constants where it did not), so activating this
-- setting changes NOTHING on its own. The one deliberate semantic change is
-- documented in the bullet_blocks note: head_max_chars becomes a qualification
-- test rather than a truncation.
--
-- The retrieval half of the config is carried over from the currently active
-- setting unchanged — this migration only ADDS the 'chunking' key.
--
-- Guarded with NOT EXISTS on the 'chunking' key, so re-running is a no-op.
-- Idempotent; transactional.

BEGIN;

-- ── new setting = active config + the 'chunking' section ────────────────────
INSERT INTO edu_ai.search_setting (label, is_active, config, notes)
SELECT
    -- fixed label: the previous label already carries its own "vN: …" chain, so
    -- concatenating it would nest ("v3: v2: v1: …"). The lineage lives in notes.
    'v3: retrieval as v2 + chunking config (bullet head repetition, sizes, heuristics)',
    false,
    s.config || jsonb_build_object('chunking', jsonb_build_object(

        'split_text_into_chunks', jsonb_build_object(
            'sizes', jsonb_build_object(
                'chunk_size',      900,
                'chunk_overlap',   50,
                'min_chunk_chars', 120,
                'hard_min_chars',  40
            ),
            'bullet_blocks', jsonb_build_object(
                'note', 'enabled=false -> a bullet block is chunked the ordinary '
                     || 'way (by size); _pack_bullets is not used. '
                     || 'repeat_head=false -> bullets are still packed as whole '
                     || 'items, but the heading is NOT prepended to each chunk. '
                     || 'The two switches are independent. head_max_chars is a '
                     || 'QUALIFICATION test, not a truncation: a candidate longer '
                     || 'than this is not a heading at all and stays in the content.',
                'enabled',            true,
                'min_bullets',        3,
                'markers',            jsonb_build_array('•', '-', '·'),
                'repeat_head',        true,
                'head_max_chars',     200,
                'head_requires_hash', false,
                'min_budget',         200
            ),
            'legal_section', jsonb_build_object(
                'enabled',                 true,
                'max_chars',               2500,
                'require_caps_first_line', true
            ),
            'sentence_boundary', jsonb_build_object(
                'max_lookahead',        80,
                'min_last_chunk_ratio', 0.4
            ),
            'short_text_prefix_max_chars', 220,
            'llm', jsonb_build_object(
                'enabled',            true,
                'max_chars',          6000,
                'min_coverage_ratio', 0.5
            )
        ),

        'validate_text_chunks', jsonb_build_object(
            'min_word_count',               4,
            'min_lang_score',               0.55,
            'langdetect_ignore_word_count', 5,
            'garbage_fragment', jsonb_build_object(
                'max_alpha_ratio', 0.25,
                'max_word_count',  15
            ),
            'bullet_fragment', jsonb_build_object(
                'min_bullet_ratio', 0.8,
                'max_word_count',   8
            ),
            'qa_pairs', jsonb_build_object(
                'min_question_lines',     2,
                'min_non_question_lines', 2,
                'non_question_min_chars', 20
            )
        )
    )),
    'Derived from the then-active setting; chunking thresholds lifted from '
    || 'component code. Values equal the previous effective behaviour except '
    || 'head_max_chars, which becomes a qualification test (see the note).'
FROM edu_ai.search_setting s
WHERE s.is_active
  AND NOT EXISTS (
      SELECT 1 FROM edu_ai.search_setting WHERE config ? 'chunking'
  );

-- ── activate it (unique partial index allows one active row: clear, then set) ─
UPDATE edu_ai.search_setting
   SET is_active = false
 WHERE is_active
   AND NOT (config ? 'chunking');

UPDATE edu_ai.search_setting
   SET is_active = true
 WHERE setting_id = (
       SELECT setting_id FROM edu_ai.search_setting
        WHERE config ? 'chunking'
        ORDER BY created_at DESC
        LIMIT 1)
   AND NOT EXISTS (SELECT 1 FROM edu_ai.search_setting WHERE is_active);

COMMIT;
