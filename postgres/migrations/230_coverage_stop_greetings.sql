-- 230: greetings and thanks out of the QA COVERAGE (qa_stopword scope='coverage').
--
-- A question that opens with a greeting lost its curated answer: "Ahoj, kdy jsou
-- přijímačky?" carries a word the curated "Kdy jsou přijímačky?" does not, and that pulls
-- the coverage under t_answer, while the same question without the greeting is answered.
-- Migration 218 took the politeness words out ("dobrý", "prosím", "děkuji"); the casual
-- greetings and thanks were left in. Like 218, the words stay in the tsquery (retrieval);
-- they only stop counting as content a curated question has to cover. An entry that is
-- nothing but a greeting still gets its exact match (qa_exact).
--
-- Kept OUT on purpose:
--   "zdravím" - its lemma is `zdravi`, the same token as "zdraví" (health)
--   "den"     - see 218 ("dobrý den" keeps it)
--
-- Measured with the regression set (lab branch lab/search-quality, 15 Nov 2026): no curated
-- question of the measured packs contains these words; sets A-N and G unchanged. With a
-- greeting or thanks around each of 66 verified questions ("Ahoj, ...", "Dobrý den, ...",
-- "... Díky"), the result equals that of the bare question in 183 of 198 cases (157
-- before); the rest is "Dobrý den" (see "den"). Such a question now also gets the bare
-- question's confident answer when that one is wrong (6 cases, 2 questions); the fix for
-- those is in the content, not here.
--
-- Terms are the lemma tokens the coverage step compares (eduai_normalize 3.4.1).
--
-- Idempotent: the rows of this migration are replaced; a word an installation already
-- holds under another note is left as it is. No transaction control.

DELETE FROM edu_ai.qa_stopword
 WHERE scope = 'coverage' AND lang = 'cs' AND note = '230: pozdravy a díky';

INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, scope, term_norm, term_lemma, term_stem)
VALUES
 ('ahoj',   'cs', true, '230: pozdravy a díky', 'coverage', 'ahoj',   'ahoj',   'ahoj'),
 ('cau',    'cs', true, '230: pozdravy a díky', 'coverage', 'cau',    'cau',    'cau'),
 ('nazdar', 'cs', true, '230: pozdravy a díky', 'coverage', 'nazdar', 'nazdar', 'nazdar'),
 ('diky',   'cs', true, '230: pozdravy a díky', 'coverage', 'diky',   'diky',   'diky'),
 ('dik',    'cs', true, '230: pozdravy a díky', 'coverage', 'dik',    'dik',    'dik')
ON CONFLICT (lang, term, scope) DO NOTHING;

-- self-check: all five are coverage stop-words (a row an operator switched off stays off)
DO $$
BEGIN
  IF (SELECT count(DISTINCT term) FROM edu_ai.qa_stopword
       WHERE scope = 'coverage' AND lang = 'cs'
         AND term IN ('ahoj', 'cau', 'nazdar', 'diky', 'dik')) <> 5 THEN
    RAISE EXCEPTION '230: greeting coverage stop-words missing';
  END IF;
END $$;
