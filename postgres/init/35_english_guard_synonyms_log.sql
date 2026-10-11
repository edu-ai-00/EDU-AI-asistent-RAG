-- last change in code by Martin Safranek (2026-07-28).
-- 35_english_guard_synonyms_log.sql
-- Closes the four bilingual-search gaps found in the readiness audit:
--   1. qa_blocklist: English terms (the guard loads ALL active terms regardless
--      of language — by design, so a Czech child typing English profanity is
--      still blocked; the gap was purely missing EN rows).
--   2. qa_synonym: English interrogative group (what/which/who/…) — excluded
--      from coverage concepts exactly like the Czech interrogatives, so an EN
--      question word cannot inflate QA match confidence.
--   3. qa_query_log.question_language: the language the query was actually
--      processed in (validated filter > detection) — needed for per-language
--      reporting and threshold calibration. answer_language already existed.
--   4. (code) rag_keywords keyword reduction language — no SQL here.
-- Idempotent; transactional.

BEGIN;

-- 1) English blocklist (norm = term; EN has no diacritics)
INSERT INTO edu_ai.qa_blocklist (term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source)
SELECT t, t, t, cat, sev, ph, true, 'en', 'seed-35' FROM (VALUES
    -- profanity
    ('fuck', 'profanity', 2, false), ('fucking', 'profanity', 2, false),
    ('shit', 'profanity', 2, false), ('bitch', 'profanity', 2, false),
    ('asshole', 'profanity', 2, false), ('cunt', 'profanity', 2, false),
    ('dick', 'profanity', 2, false), ('bastard', 'profanity', 2, false),
    ('whore', 'profanity', 2, false), ('slut', 'profanity', 2, false),
    ('motherfucker', 'profanity', 2, false), ('wanker', 'profanity', 2, false),
    -- self-harm (phrases matched on the normalized query)
    ('suicide', 'self_harm', 3, false),
    -- POZOR: term_norm frází musí být v PIPELINE-normalizované podobě (bez
    -- stopslov) — guard hledá frázi v normalizovaném dotazu ("want to die" by
    -- se v "want die" nikdy nenašlo)
    ('kill myself', 'self_harm', 3, true),
    ('want die', 'self_harm', 3, true),
    ('hurt myself', 'self_harm', 3, true),
    ('end life', 'self_harm', 3, true),
    ('self harm', 'self_harm', 3, true),
    -- hate
    ('nigger', 'hate', 3, false), ('faggot', 'hate', 3, false), ('retard', 'hate', 3, false),
    -- sexual
    ('porn', 'sexual', 2, false), ('porno', 'sexual', 2, false), ('blowjob', 'sexual', 2, false)
) AS v(t, cat, sev, ph)
WHERE NOT EXISTS (SELECT 1 FROM edu_ai.qa_blocklist WHERE lang = 'en');

-- 2) English interrogatives (identity forms — no inflection in EN wh-words)
INSERT INTO edu_ai.qa_synonym (group_id, kind, term, term_norm, term_lemma, term_stem, is_key, lang, source, is_active)
SELECT 'syn_q_en_wh', 'interrogative', t, t, t, t, (t = 'what'), 'en', 'seed-35', true
FROM unnest(ARRAY['what','which','who','whom','whose','when','where','why','how']) AS t
WHERE NOT EXISTS (SELECT 1 FROM edu_ai.qa_synonym WHERE group_id = 'syn_q_en_wh');

-- 3) question language in the audit log
ALTER TABLE edu_ai.qa_query_log ADD COLUMN IF NOT EXISTS question_language text;

COMMIT;
