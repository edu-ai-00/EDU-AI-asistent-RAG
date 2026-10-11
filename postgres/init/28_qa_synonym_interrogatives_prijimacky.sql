-- last change in code by Martin Safranek (2026-07-26).
-- 28_qa_synonym_interrogatives_prijimacky.sql
-- Fixes for the false-positive QA match found in web-chat testing (2026-07-26):
-- the query "jaký je termín příjmaček?" confidently answered a subscription-FAQ
-- entry ("Jak funguje výběr termínů?") because
--   (a) the interrogative "jaký" counted as a content concept in coverage
--       (2/3 = 0.667 ≥ t_answer 0.60 → band A) — the code EXCLUDES tokens of
--       kind='interrogative' synonym groups from coverage, but NO such groups
--       were seeded, and
--   (b) the typo "příjmaček" reduces to prijmacka/prijmacek — outside the
--       prijimacky family — so the CORRECT entries scored zero on the domain
--       word (tech-debt C7) and no entity group existed to bridge it.
--
-- Seeds (data only — the qa_common.expand_query mechanism already handles both):
--   1. kind='interrogative' groups: excluded from coverage concepts (they still
--      match in the tsquery; they are never expanded — by design).
--   2. kind='entity' group syn_prijimacky: canonical + colloquial + the common
--      missing-í typo family; matching any of them expands the tsquery to the
--      whole family, so typos reach the right entries.
--   3. One-off cleanup of [p]/[/p] pseudo-tags in 4 answer_md rows (to-das FAQ
--      export artifact); the QA JSON reader now strips them at ingest, this
--      fixes rows already in the DB.
-- Forms (term_norm/lemma/stem) generated with the shared eduai_normalize package
-- for query↔index parity. Idempotent per group_id / regexp. Transactional.

BEGIN;

INSERT INTO edu_ai.qa_synonym (group_id, kind, term, term_norm, term_lemma, term_stem, is_key, lang, source, is_active)
SELECT * FROM (VALUES
    ('syn_q_kdo',   'interrogative', 'kdo',    'kdo',    'kdo',   'kdo',  true,  'cs', 'seed-28', true),
    ('syn_q_kdo',   'interrogative', 'komu',   'komu',   'kdo',   'kom',  false, 'cs', 'seed-28', true),
    ('syn_q_co',    'interrogative', 'co',     'co',     'co',    'co',   true,  'cs', 'seed-28', true),
    ('syn_q_co',    'interrogative', 'čeho',   'ceho',   'co',    'ceh',  false, 'cs', 'seed-28', true),
    ('syn_q_co',    'interrogative', 'čím',    'cim',    '',      'cim',  false, 'cs', 'seed-28', true),
    ('syn_q_kdy',   'interrogative', 'kdy',    'kdy',    'kdy',   'kdy',  true,  'cs', 'seed-28', true),
    ('syn_q_kde',   'interrogative', 'kde',    'kde',    'kde',   'kde',  true,  'cs', 'seed-28', true),
    ('syn_q_kde',   'interrogative', 'kam',    'kam',    'kama',  'kam',  false, 'cs', 'seed-28', true),
    ('syn_q_jak',   'interrogative', 'jak',    'jak',    'jak',   'jak',  true,  'cs', 'seed-28', true),
    ('syn_q_jaky',  'interrogative', 'jaký',   'jaky',   'jaky',  'jak',  true,  'cs', 'seed-28', true),
    ('syn_q_jaky',  'interrogative', 'jaká',   'jaka',   'jaky',  'jak',  false, 'cs', 'seed-28', true),
    ('syn_q_jaky',  'interrogative', 'jaké',   'jake',   'jaky',  'jak',  false, 'cs', 'seed-28', true),
    ('syn_q_jaky',  'interrogative', 'jakou',  'jakou',  'jaky',  'jak',  false, 'cs', 'seed-28', true),
    ('syn_q_jaky',  'interrogative', 'jakých', 'jakych', 'jaky',  'jak',  false, 'cs', 'seed-28', true),
    ('syn_q_jaky',  'interrogative', 'jakém',  'jakem',  'jaky',  '',     false, 'cs', 'seed-28', true),
    ('syn_q_ktery', 'interrogative', 'který',  'ktery',  'ktery', 'kter', true,  'cs', 'seed-28', true),
    ('syn_q_ktery', 'interrogative', 'která',  'ktera',  'ktery', 'kter', false, 'cs', 'seed-28', true),
    ('syn_q_ktery', 'interrogative', 'které',  'ktere',  'ktery', 'kter', false, 'cs', 'seed-28', true),
    ('syn_q_proc',  'interrogative', 'proč',   'proc',   'proc',  'proc', true,  'cs', 'seed-28', true),
    ('syn_q_kolik', 'interrogative', 'kolik',  'kolik',  'kolik', 'kolik',true,  'cs', 'seed-28', true)
) AS v(group_id, kind, term, term_norm, term_lemma, term_stem, is_key, lang, source, is_active)
WHERE NOT EXISTS (SELECT 1 FROM edu_ai.qa_synonym WHERE group_id LIKE 'syn_q_%');

INSERT INTO edu_ai.qa_synonym (group_id, kind, term, term_norm, term_lemma, term_stem, is_key, lang, source, is_active)
SELECT * FROM (VALUES
    ('syn_prijimacky', 'entity', 'přijímačky',  'prijimacky', 'prijimac',  'prijimack',  true,  'cs', 'seed-28', true),
    ('syn_prijimacky', 'entity', 'přijímačka',  'prijimacka', 'prijimac',  'prijimack',  false, 'cs', 'seed-28', true),
    ('syn_prijimacky', 'entity', 'přijímaček',  'prijimacek', 'prijimac',  'prijimacek', false, 'cs', 'seed-28', true),
    ('syn_prijimacky', 'entity', 'přijímací',   'prijimaci',  'prijimaci', 'prijimac',   false, 'cs', 'seed-28', true),
    -- common missing-í typo family (příjmačky → prijmack*): bridges typos to the entries
    ('syn_prijimacky', 'entity', 'příjmačky',   'prijmacky',  'prijmacka', 'prijmack',   false, 'cs', 'seed-28', true),
    ('syn_prijimacky', 'entity', 'příjmačka',   'prijmacka',  'prijmacka', 'prijmack',   false, 'cs', 'seed-28', true),
    ('syn_prijimacky', 'entity', 'příjmaček',   'prijmacek',  'prijmacka', 'prijmacek',  false, 'cs', 'seed-28', true)
) AS v(group_id, kind, term, term_norm, term_lemma, term_stem, is_key, lang, source, is_active)
WHERE NOT EXISTS (SELECT 1 FROM edu_ai.qa_synonym WHERE group_id = 'syn_prijimacky');

-- one-off cleanup of [p]/[/p] pseudo-tags in already-ingested curated answers
UPDATE edu_ai.qa_entries
SET answer_md = btrim(regexp_replace(answer_md, '\[/?p\]', '', 'g'))
WHERE answer_md ~ '\[/?p\]';

COMMIT;
