-- 229: questions about the assistant itself ("how old are you", "how are you", "what is your
-- name") get the capabilities answer, for every organisation.
--
-- These questions are about the assistant, not about a school, so a school's QA pack is the
-- wrong place for them: an entry like "Kolik ti je let?" exists only where someone wrote it,
-- and as a curated question it can show up among the offered questions of real ones. The
-- meta list (qa_blocklist category 'meta') is global and is checked before the cascade by
-- route._meta_check: the WHOLE folded query (diacritics and punctuation removed, at most
-- `meta_max_words` words) must equal a phrase, also after the meta fillers (qa_stopword
-- scope 'meta_filler', e.g. "ti", "už") are dropped. So "Jak se ti daří?" matches
-- "jak se dari", while "Kolik let trvá povinná docházka?" matches nothing. The answer is the
-- existing system_message 'capabilities' in the query's language.
--
-- Checked before release: no query of the regression sets (A, B, C, D, L, N, G) equals a
-- phrase; in the production query log (Aug-Oct 2026) 2 queries do, both small talk; in a
-- sample of 3,361 conversational messages from a children's chat 14 do (how are you, how
-- old are you, are you alive) and 4 more ask the assistant's name.
--
-- Rows of this migration are marked source '229'. A phrase already in the meta list is not
-- inserted twice. Undo: DELETE FROM edu_ai.qa_blocklist WHERE source = '229'.
--
-- Idempotent. No transaction control (runner, from 217).

DELETE FROM edu_ai.qa_blocklist WHERE category = 'meta' AND source = '229';

INSERT INTO edu_ai.qa_blocklist
       (term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source,
        term_stem, use_norm, use_lemma, use_stem)
SELECT v.term, v.term_norm, NULL, 'meta', 0, true, true, v.lang, '229',
       v.term_norm, true, false, false
  FROM (VALUES
        -- cs: age, alive, how are you
        ('kolik ti je',         'kolik ti je',         'cs'),
        ('kolik ti je let',     'kolik ti je let',     'cs'),
        ('kolik je ti let',     'kolik je ti let',     'cs'),
        ('kolik máš let',       'kolik mas let',       'cs'),
        ('jak jsi starý',       'jak jsi stary',       'cs'),
        ('jak jsi stará',       'jak jsi stara',       'cs'),
        ('žiješ',               'zijes',               'cs'),
        ('jak se máš',          'jak se mas',          'cs'),
        ('jak se máte',         'jak se mate',         'cs'),
        ('jak se dnes máš',     'jak se dnes mas',     'cs'),
        ('jak se máš dneska',   'jak se mas dneska',   'cs'),
        ('jak je',              'jak je',              'cs'),
        ('jak se vede',         'jak se vede',         'cs'),
        ('jak se daří',         'jak se dari',         'cs'),
        -- cs: the assistant's name (the answer introduces it, migration 233)
        ('jak se jmenuješ',     'jak se jmenujes',     'cs'),
        ('jak se jmenujete',    'jak se jmenujete',    'cs'),
        ('jaké máš jméno',      'jake mas jmeno',      'cs'),
        ('jaké je tvoje jméno', 'jake je tvoje jmeno', 'cs'),
        -- en, de, es: the same questions in the other languages the code supports
        ('how old are you',     'how old are you',     'en'),
        ('how are you',         'how are you',         'en'),
        ('are you alive',       'are you alive',       'en'),
        ('what is your name',   'what is your name',   'en'),
        ('what''s your name',   'what s your name',    'en'),
        ('wie alt bist du',     'wie alt bist du',     'de'),
        ('wie geht es dir',     'wie geht es dir',     'de'),
        ('lebst du',            'lebst du',            'de'),
        -- route._fold_q drops "ß" (not in its letter class), so "heißt" folds to "hei t"
        ('wie heißt du',        'wie hei t du',        'de'),
        ('wie heisst du',       'wie heisst du',       'de'),
        ('cuántos años tienes', 'cuantos anos tienes', 'es'),
        ('cómo estás',          'como estas',          'es'),
        ('estás vivo',          'estas vivo',          'es'),
        ('cómo te llamas',      'como te llamas',      'es')
       ) AS v(term, term_norm, lang)
 WHERE NOT EXISTS (SELECT 1 FROM edu_ai.qa_blocklist b
                    WHERE b.category = 'meta' AND b.is_active
                      AND b.term_norm = v.term_norm AND b.lang = v.lang);

-- self-check: every phrase of this migration is an active meta phrase
DO $$
DECLARE
  missing text;
BEGIN
  SELECT string_agg(p, ', ') INTO missing
    FROM unnest(ARRAY['kolik ti je', 'kolik ti je let', 'kolik je ti let', 'kolik mas let',
                      'jak jsi stary', 'jak jsi stara', 'zijes', 'jak se mas', 'jak se mate',
                      'jak se dnes mas', 'jak se mas dneska', 'jak je', 'jak se vede',
                      'jak se dari', 'how old are you', 'how are you', 'are you alive',
                      'wie alt bist du', 'wie geht es dir', 'lebst du', 'cuantos anos tienes',
                      'como estas', 'estas vivo', 'jak se jmenujes', 'jak se jmenujete',
                      'jake mas jmeno', 'jake je tvoje jmeno', 'what is your name',
                      'what s your name', 'wie hei t du', 'wie heisst du',
                      'como te llamas']) AS p
   WHERE NOT EXISTS (SELECT 1 FROM edu_ai.qa_blocklist b
                      WHERE b.category = 'meta' AND b.is_active AND b.term_norm = p);
  IF missing IS NOT NULL THEN
    RAISE EXCEPTION '229: meta phrases missing: %', missing;
  END IF;
END $$;
