-- 216: school_identity (2026-09-18, Jan).
-- "Jak se jmenuje naše škola?" was no_answer with "zkus to přeformulovat" — a
-- rephrasing that can never work. Measured on production 2026-09-11 (log 3926,
-- 3927): the name lives in legal_entity.display_name and in NO searchable text,
-- so no wording reaches it. ZS_KAVCI_HORY_ZS proves it is not a corpus gap —
-- 10 of its chunks name the school and "jak se jmenuje škola?" (log 3914) still
-- ended no_answer: `skola` exceeds bm25_max_df_share 0.25 and `jmenovat` has
-- df 0, so both retrieval pools came back empty.
--
-- Same shape as migration 124 (meta): detection is DATA in qa_blocklist, the
-- answer is a system_message with {{SCHOOL}} substituted by route.py from the
-- legal_entity row. Deactivating these rows disables the path — no redeploy.
--
-- Idempotent; transactional.

BEGIN;

-- 'identity' rows are DETECTION vocabulary like 'meta', not blocked terms:
-- severity 0, and route._load_blocklist excludes the category from the matcher.
ALTER TABLE edu_ai.qa_blocklist DROP CONSTRAINT IF EXISTS qa_blocklist_category_check;
ALTER TABLE edu_ai.qa_blocklist ADD CONSTRAINT qa_blocklist_category_check
    CHECK (category = ANY (ARRAY['profanity'::text, 'hate'::text,
                                 'sexual'::text, 'self_harm'::text,
                                 'violence'::text, 'meta'::text,
                                 'identity'::text]));

-- the answer, 4 languages
INSERT INTO edu_ai.system_message (message_key, lang, message, created_by, notes)
SELECT v.k, v.l, v.m, 'init-216', 'identita školy; {{SCHOOL}} doplňuje kód z legal_entity'
FROM (VALUES
  ('school_identity', 'cs', 'Tahle škola se jmenuje {{SCHOOL}}.'),
  ('school_identity', 'en', 'This school is called {{SCHOOL}}.'),
  ('school_identity', 'de', 'Diese Schule heißt {{SCHOOL}}.'),
  ('school_identity', 'es', 'Esta escuela se llama {{SCHOOL}}.')
) AS v(k, l, m)
WHERE NOT EXISTS (SELECT 1 FROM edu_ai.system_message
                  WHERE message_key = v.k AND lang = v.l);

-- detection phrases (folded, whole-query match in route._identity_check).
-- Deliberately only wordings whose ONLY subject is the school itself: "jak se
-- jmenuje školník" is a corpus question and must stay on the cascade.
INSERT INTO edu_ai.qa_blocklist (term, term_norm, term_lemma, category, severity, is_phrase, lang, source)
SELECT v.t, v.t, NULL, 'identity', 0, true, v.l, 'init-216'
FROM (VALUES
  ('jak se jmenuje nase skola', 'cs'), ('jak se jmenuje skola', 'cs'),
  ('jak se jmenuje tato skola', 'cs'), ('jak se jmenuje vase skola', 'cs'),
  ('jak se jmenujete', 'cs'), ('jak se jmenuje nase zakladni skola', 'cs'),
  ('nazev skoly', 'cs'), ('nazev nasi skoly', 'cs'), ('jmeno skoly', 'cs'),
  ('jaky je nazev skoly', 'cs'), ('jaky je nazev nasi skoly', 'cs'),
  ('na jake jsem skole', 'cs'), ('na jakou skolu chodim', 'cs'),
  ('do jake skoly chodim', 'cs'), ('ktera skola to je', 'cs'),
  ('what is the name of the school', 'en'), ('what is this school called', 'en'),
  ('what school is this', 'en'), ('school name', 'en'),
  ('wie heisst die schule', 'de'), ('wie heisst unsere schule', 'de'),
  ('name der schule', 'de'),
  ('como se llama la escuela', 'es'), ('como se llama nuestra escuela', 'es'),
  ('nombre de la escuela', 'es')
) AS v(t, l)
WHERE NOT EXISTS (SELECT 1 FROM edu_ai.qa_blocklist
                  WHERE term_norm = v.t AND category = 'identity');

COMMIT;
