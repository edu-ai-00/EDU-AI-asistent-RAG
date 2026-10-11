-- last change in code by Martin Safranek (2026-07-29).
-- 44_capture_live_config.sql
-- Capture three configuration states that were created by ad-hoc SQL on the live
-- database and never made it into a migration — so a clean install would silently
-- differ from the environment everything was verified on (found by auditing the
-- live DB against postgres/init/ on 2026-07-29):
--
--   1. prompt markdown_chunker v2 — a change of language is a HARD chunk
--      boundary. The belt to LanguageSegmenter's braces: the segmenter guarantees
--      chunker input is monolingual per row, the prompt rule covers a language
--      switch INSIDE one line, which the line-based segmenter cannot see.
--   2. search_setting v6 — v5 + answer step model gpt-4.1-nano (the measured
--      A/B winner; ~4x faster document answers). ACTIVE.
--   3. search_setting v7 — v6 + German and Spanish reducers. INACTIVE: it exists
--      so that activating those languages is one UPDATE, and as the proof that a
--      new language needs config rows, not code (verified end to end 2026-07-29:
--      snowball_german / snowball_spanish resolved purely from these rows).
--
-- Everything is guarded with NOT EXISTS, so on the live database that already
-- has these rows this migration is a no-op. Active flags are only granted when
-- the row is created here — an existing installation keeps whatever is active.
-- Idempotent; transactional.

BEGIN;

-- ── 1) markdown_chunker v2: language change = hard chunk boundary ───────────
INSERT INTO edu_ai.prompt_registry
    (prompt_key, version, prompt_text, is_active, component, description,
     placeholders, model_hint, lang, created_by, notes)
SELECT prompt_key, version + 1,
       prompt_text || E'\n        A change of LANGUAGE is a hard boundary: never'
                   || E' put text in two different languages in the same piece.'
                   || E' Split exactly where the language changes, even if the'
                   || E' piece is far below max_chars.',
       false, component,
       'Language change = hard chunk boundary (mixed-language chunks got one stemmer).',
       placeholders, model_hint, lang, 'lang-boundary-2026-07-29',
       'Derived from v' || version || '; previous version kept inactive for rollback.'
FROM edu_ai.prompt_registry p
WHERE is_active AND prompt_key = 'markdown_chunker'
  AND NOT EXISTS (SELECT 1 FROM edu_ai.prompt_registry x
                  WHERE x.prompt_key = 'markdown_chunker'
                    AND x.created_by = 'lang-boundary-2026-07-29');

UPDATE edu_ai.prompt_registry SET is_active = false
WHERE prompt_key = 'markdown_chunker' AND is_active
  AND EXISTS (SELECT 1 FROM edu_ai.prompt_registry
              WHERE prompt_key = 'markdown_chunker'
                AND created_by = 'lang-boundary-2026-07-29' AND NOT is_active);
UPDATE edu_ai.prompt_registry SET is_active = true
WHERE prompt_key = 'markdown_chunker'
  AND created_by = 'lang-boundary-2026-07-29'
  AND NOT EXISTS (SELECT 1 FROM edu_ai.prompt_registry
                  WHERE prompt_key = 'markdown_chunker' AND is_active);

-- ── 2) v6: v5 + answer model gpt-4.1-nano (A/B winner), ACTIVE ──────────────
INSERT INTO edu_ai.search_setting (setting_id, label, config, is_active, notes)
SELECT gen_random_uuid(),
       'v6 EXPERIMENT: v5 + answer model gpt-4.1-nano',
       jsonb_set(config, '{pipeline}',
           (SELECT jsonb_agg(CASE WHEN step->>'step' = 'answer'
                THEN jsonb_set(step, '{params,model}', '"gpt-4.1-nano"')
                ELSE step END)
            FROM jsonb_array_elements(config->'pipeline') AS step)),
       false,
       'A/B: nano vs mini for answer synthesis. Local experiment 2026-07-26; '
       'nano measured ~4x faster with equal answer quality on the FAQ set.'
FROM edu_ai.search_setting
WHERE label LIKE 'v5:%'
  AND NOT EXISTS (SELECT 1 FROM edu_ai.search_setting WHERE label LIKE 'v6 EXPERIMENT%');

-- activate v6 ONLY on a fresh install (nothing else active)
UPDATE edu_ai.search_setting SET is_active = true
WHERE label LIKE 'v6 EXPERIMENT%'
  AND NOT EXISTS (SELECT 1 FROM edu_ai.search_setting WHERE is_active);

-- ── 3) v7: v6 + de/es reducers, INACTIVE (one UPDATE away) ──────────────────
INSERT INTO edu_ai.search_setting (setting_id, label, config, is_active, notes)
SELECT gen_random_uuid(),
       'v7 TEST: v6 + de/es engines (multilingual end-to-end test)',
       jsonb_set(config, '{engines}', (config->'engines') || jsonb_build_array(
           jsonb_build_object('kind','lemmatizer','name','simplemma','language','de',
                              'column','lemma_text',
                              'active', jsonb_build_object('ingest_qa',true,'ingest_docs',true)),
           jsonb_build_object('kind','stemmer','name','snowball_german','language','de',
                              'column','stem_text',
                              'params', jsonb_build_object('min_len',3),
                              'active', jsonb_build_object('ingest_qa',true,'ingest_docs',true)),
           jsonb_build_object('kind','lemmatizer','name','simplemma','language','es',
                              'column','lemma_text',
                              'active', jsonb_build_object('ingest_qa',true,'ingest_docs',true)),
           jsonb_build_object('kind','stemmer','name','snowball_spanish','language','es',
                              'column','stem_text',
                              'params', jsonb_build_object('min_len',3),
                              'active', jsonb_build_object('ingest_qa',true,'ingest_docs',true))
       )),
       false,
       'de-es-test | Adds de/es reducers. snowball_german / snowball_spanish need NO '
       'code: the library parses snowball_<language>. Verified end to end 2026-07-29.'
FROM edu_ai.search_setting
WHERE label LIKE 'v6 EXPERIMENT%'
  AND NOT EXISTS (SELECT 1 FROM edu_ai.search_setting WHERE notes LIKE 'de-es-test%');

COMMIT;
