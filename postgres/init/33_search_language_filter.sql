-- last change in code by Martin Safranek (2026-07-27).
-- 33_search_language_filter.sql
-- Language filter for the search API (agreed 2026-07-27):
--   1. The language číselník keeps only TWO active languages — cs and en.
--      (Single source of truth: this also narrows ingest validation and the
--      classification LLM's allowed codes automatically.)
--   2. search_setting v5 engines: 2 → 4 — the Czech lemmatizer/stemmer pair
--      gains English counterparts, each engine tagged with the `language`
--      code matching the číselník. The EN pair is DECLARED (language-aware
--      config shape) but active:false until the EN reduction is wired into
--      eduai_normalize/ChunkLemmatizer (tech-debt F1).
--   3. prompt v4: the language instruction loses the multi/unknown escape
--      hatches (those codes are no longer active) — forced choice between
--      the listed codes.
-- The /search `language` parameter itself is code (search service).
-- Idempotent; language deactivation is reversible (is_active=true).

BEGIN;

-- 1) only cs + en stay active
UPDATE edu_ai.code_list SET is_active = false
WHERE list_code = 'language' AND code NOT IN ('cs', 'en') AND is_active;

-- 2) four language-tagged engines on the active v5
UPDATE edu_ai.search_setting
SET config = jsonb_set(config, '{engines}', $ENG$[
  {"kind": "lemmatizer", "name": "simplemma", "language": "cs",
   "column": "lemma_text", "active": {"ingest_qa": true, "ingest_docs": true}},
  {"kind": "stemmer", "name": "dolamic_savoy_light", "language": "cs",
   "column": "stem_text", "params": {"min_len": 3, "aggressive": false},
   "active": {"ingest_qa": true, "ingest_docs": true}},
  {"kind": "lemmatizer", "name": "simplemma", "language": "en",
   "column": "lemma_text", "active": {"ingest_qa": false, "ingest_docs": false},
   "notes": "declared for the EN rollout; reduction wiring = tech-debt F1"},
  {"kind": "stemmer", "name": "snowball_english", "language": "en",
   "column": "stem_text", "active": {"ingest_qa": false, "ingest_docs": false},
   "notes": "declared for the EN rollout; reduction wiring = tech-debt F1"}
]$ENG$::jsonb)
WHERE label ~ '^v[56]'   -- v5 baseline + the v6 nano experiment copy
  AND jsonb_array_length(config->'engines') = 2;

-- 3) prompt v4: forced-choice language instruction (multi/unknown inactive)
INSERT INTO edu_ai.prompt_registry
    (prompt_key, version, prompt_text, is_active, component, description,
     placeholders, model_hint, lang, created_by, notes)
SELECT prompt_key, 4,
       replace(prompt_text,
'Return the code of the DOMINANT language of the text; ''multi'' only when the document is genuinely multilingual; ''unknown'' when undeterminable.',
'Return the code of the DOMINANT language of the text — exactly one of the codes above.'),
       false, component,
       'v4: language instruction = forced choice (číselník narrowed to cs+en, multi/unknown deactivated).',
       placeholders, model_hint, lang, 'migration-33',
       'Derived from v3; v3 kept inactive for rollback.'
FROM edu_ai.prompt_registry
WHERE prompt_key = 'document_metadata_extraction' AND version = 3
  AND NOT EXISTS (SELECT 1 FROM edu_ai.prompt_registry
                  WHERE prompt_key = 'document_metadata_extraction' AND version = 4);

UPDATE edu_ai.prompt_registry SET is_active = false
WHERE prompt_key = 'document_metadata_extraction' AND version < 4 AND is_active;
UPDATE edu_ai.prompt_registry SET is_active = true
WHERE prompt_key = 'document_metadata_extraction' AND version = 4 AND NOT is_active;

COMMIT;
