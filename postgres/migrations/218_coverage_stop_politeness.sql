-- 218: politeness and filler words out of the QA COVERAGE (qa_stopword scope='coverage').
--
-- A polite question named the same thing as a curated one and still missed it:
-- "Dobrý den, chtěla bych se zeptat, kdy se konají opravné zkoušky? Předem
-- děkuji." carries five words the curated "Kdy se opravné zkoušky konají?"
-- does not, and they pull the coverage under t_answer. Measured with
-- tools/lab/variants/compare_forms.py (2 097 written variants of the school's
-- 210 curated questions, form norm+lemma+stem): polite questions 0 % → 25 %
-- confident right answers, curt ones 35 % → 42 %, all variants 993 → 1 055,
-- confident wrong answers 12 → 11. The words stay in the tsquery (retrieval);
-- they only stop counting as content the curated question has to cover.
--
-- Kept OUT on purpose:
--   "den"  — "Co dělat při nemoci v den JPZ?" needs it (+31 more variants
--            measured with it, but the risk is a real question's content word)
--   "rád"  — folds to `rad`, the same token as "řád" (školní řád)
--   "moci", "mít" (mít is already there) — modal verbs a curated question may
--            hinge on are left to the IDF weights
-- "dobrý" is in: alone it is a grade (trojka), but its synonym group was not
-- made for that reason (217) and a grade question carries the grade's subject
-- words; checked by the replay of the production log before release.
--
-- Terms are the lemma tokens the coverage step compares (eduai_normalize 3.3.0,
-- the active normalization config, 2026-10-26).
--
-- Idempotent: the rows of this migration are replaced. No transaction control.

DELETE FROM edu_ai.qa_stopword
 WHERE scope = 'coverage' AND lang = 'cs' AND note = '218: zdvořilost a výplňová slova';

INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, scope, term_norm, term_lemma, term_stem)
VALUES
 -- politeness
 ('dobry',     'cs', true, '218: zdvořilost a výplňová slova', 'coverage', 'dobry',     'dobry',     'dobr'),
 ('dobro',     'cs', true, '218: zdvořilost a výplňová slova', 'coverage', 'dobro',     'dobro',     'dobr'),
 ('prosit',    'cs', true, '218: zdvořilost a výplňová slova', 'coverage', 'prosim',    'prosit',    'prosit'),
 ('zeptat',    'cs', true, '218: zdvořilost a výplňová slova', 'coverage', 'zeptat',    'zeptat',    'zept'),
 ('dekovat',   'cs', true, '218: zdvořilost a výplňová slova', 'coverage', 'dekuji',    'dekovat',   'dekuj'),
 ('podekovat', 'cs', true, '218: zdvořilost a výplňová slova', 'coverage', 'podekovat', 'podekovat', 'podekov'),
 ('zda',       'cs', true, '218: zdvořilost a výplňová slova', 'coverage', 'zda',       'zda',       'zda'),
 ('zdali',     'cs', true, '218: zdvořilost a výplňová slova', 'coverage', 'zdali',     'zdali',     'zdal'),
 ('predem',    'cs', true, '218: zdvořilost a výplňová slova', 'coverage', 'predem',    'predem',    ''),
 ('dovolit',   'cs', true, '218: zdvořilost a výplňová slova', 'coverage', 'dovolit',   'dovolit',   'dovolit'),
 ('mnohokrat', 'cs', true, '218: zdvořilost a výplňová slova', 'coverage', 'mnohokrat', 'mnohokrat', 'mnohokrat'),
 ('zdvorile',  'cs', true, '218: zdvořilost a výplňová slova', 'coverage', 'zdvorile',  'zdvorile',  'zdvoril'),
 ('chtit',     'cs', true, '218: zdvořilost a výplňová slova', 'coverage', 'chtit',     'chtit',     'chtit'),
 ('velmi',     'cs', true, '218: zdvořilost a výplňová slova', 'coverage', 'velmi',     'velmi',     'vel'),
 ('laskavy',   'cs', true, '218: zdvořilost a výplňová slova', 'coverage', 'laskavy',   'laskavy',   'laskav'),
 -- fillers of an impatient question
 ('teda',      'cs', true, '218: zdvořilost a výplňová slova', 'coverage', 'teda',      'teda',      ''),
 ('furt',      'cs', true, '218: zdvořilost a výplňová slova', 'coverage', 'furt',      'furt',      'furt'),
 ('proste',    'cs', true, '218: zdvořilost a výplňová slova', 'coverage', 'proste',    'proste',    'prost'),
 ('rychle',    'cs', true, '218: zdvořilost a výplňová slova', 'coverage', 'rychle',    'rychle',    'rychl'),
 ('nikde',     'cs', true, '218: zdvořilost a výplňová slova', 'coverage', 'nikde',     'nikde',     'nikd'),
 ('porad',     'cs', true, '218: zdvořilost a výplňová slova', 'coverage', 'porad',     'porad',     'porad')
ON CONFLICT (lang, term, scope) DO NOTHING;

-- self-check: every word present, and the two collisions stayed out
DO $$
DECLARE
  n int;
BEGIN
  SELECT count(*) INTO n FROM edu_ai.qa_stopword
   WHERE scope = 'coverage' AND lang = 'cs' AND is_active
     AND term IN ('dobry','dobro','prosit','zeptat','dekovat','podekovat','zda','zdali','predem',
                  'dovolit','mnohokrat','zdvorile','chtit','velmi','laskavy',
                  'teda','furt','proste','rychle','nikde','porad');
  IF n <> 21 THEN
    RAISE EXCEPTION '218: % of 21 coverage stop-words present', n;
  END IF;
  IF EXISTS (SELECT 1 FROM edu_ai.qa_stopword
              WHERE scope = 'coverage' AND lang = 'cs' AND is_active AND term IN ('rad', 'den')) THEN
    RAISE EXCEPTION '218: "rad" (= řád) or "den" is a coverage stop-word';
  END IF;
END $$;
