-- last change in code by Martin Safranek (2026-07-27).
-- 34_english_engines.sql
-- Turn ON the English reduction (closes the wiring part of tech-debt F1):
--   1. seed English stopwords into qa_stopword (lang='en') — the authoritative
--      per-language stoplists loaded by settings.py (query side) and the
--      ChunkLemmatizer _load_ctx (index side); interrogatives (what/which/who/
--      when/where/why/how) are intentionally NOT stopwords, same design as cs.
--   2. flip the two declared EN engines (simplemma en + snowball_english) to
--      active:true on v5 + the active v6 copy — eduai_normalize 1.1.0 now
--      implements them (lang param; snowballstemmer dependency).
-- Idempotent. NOTE: EN documents ingested BEFORE this migration have
-- cs-reduced tokens baked into their chunk_lemma/stem_text — re-ingest them.

BEGIN;

INSERT INTO edu_ai.qa_stopword (lang, term, is_active)
SELECT 'en', t, true FROM unnest(ARRAY[
    'a','an','the','and','or','but','if','then','else','so','than',
    'of','at','by','for','with','about','against','between','into',
    'through','during','before','after','above','below','to','from',
    'up','down','in','out','on','off','over','under','again','once',
    'here','there','all','any','both','each','few','more','most',
    'other','some','such','no','nor','not','only','own','same','too',
    'very','can','will','just','should','would','could','may','might',
    'must','shall','now',
    'is','are','was','were','be','been','being','am',
    'have','has','had','having','do','does','did','doing',
    'i','you','he','she','it','we','they','me','him','her','us',
    'them','my','your','his','its','our','their','mine','yours',
    'this','that','these','those','as','s','t'
]) AS t
WHERE NOT EXISTS (SELECT 1 FROM edu_ai.qa_stopword WHERE lang = 'en');

UPDATE edu_ai.search_setting
SET config = jsonb_set(config, '{engines}', (
    SELECT jsonb_agg(
        CASE WHEN e->>'language' = 'en'
             THEN jsonb_set(e, '{active}', '{"ingest_qa": true, "ingest_docs": true}'::jsonb)
                  - 'notes'
             ELSE e END)
    FROM jsonb_array_elements(config->'engines') e))
WHERE label ~ '^v[56]'
  AND EXISTS (SELECT 1 FROM jsonb_array_elements(config->'engines') e
              WHERE e->>'language' = 'en'
                AND (e->'active'->>'ingest_docs')::boolean = false);

COMMIT;
