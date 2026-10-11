-- last change in code by Martin Safranek (2026-08-01).
-- 60_qa_stopword_scope.sql
-- qa_stopword.scope — one číselník, two jobs.
--
-- There were two stoplists with different semantics and only one of them was
-- data: qa_stopword (a token is dropped at tokenization, so it never reaches the
-- index) and _COVERAGE_STOP hardcoded in qa_common.py (a token stays in the
-- tsquery but does not count towards coverage). The second is why "Jaké mám mít
-- pomůcky?" fails: its concepts are [mama] [mit] [pomucka], `pomucka` matches
-- the section heading, and coverage comes out 1/3 = 0.333 — under min_coverage
-- 0.5 — because the other two are forms of the empty auxiliary "mít" ('mama'
-- being simplemma's wrong lemma for "mám").
--
--   scope='index'     dropped at tokenization (previous behaviour, the default)
--   scope='coverage'  kept in the tsquery, excluded from the coverage ratio
--
-- The distinction matters: "mít" SHOULD still help retrieve, it just must not
-- raise confidence. Putting it in scope='index' would remove it from search
-- altogether.
--
-- The cs rows are the previous in-code set plus `mit` and `mama`. The en/de/es
-- rows are the obvious auxiliaries and articles and WANT REVIEW — the in-code
-- set was applied to every language while being almost entirely Czech.
--
-- Idempotent; transactional.

BEGIN;

ALTER TABLE edu_ai.qa_stopword
    ADD COLUMN IF NOT EXISTS scope text NOT NULL DEFAULT 'index';

COMMENT ON COLUMN edu_ai.qa_stopword.scope IS
    'index = dropped at tokenization; coverage = kept in the tsquery but not '
    'counted towards coverage. See migration 60.';

-- a term may need to exist in both scopes, so it joins the key
ALTER TABLE edu_ai.qa_stopword DROP CONSTRAINT IF EXISTS qa_stopword_pkey;
ALTER TABLE edu_ai.qa_stopword ADD PRIMARY KEY (lang, term, scope);

INSERT INTO edu_ai.qa_stopword (lang, term, scope, note) VALUES
  ('cs', 'a', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'ale', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'bude', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'budou', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'byl', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'byla', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'bylo', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'byt', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'co', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'do', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'i', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'ja', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'je', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'jsou', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'k', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'ke', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'mama', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'me', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'mi', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'mit', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'muj', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'my', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'na', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'nad', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'nebo', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'o', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'od', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'on', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'ona', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'ono', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'po', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'pod', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'pri', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'pro', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 's', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'se', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'si', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'ta', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'tak', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'ten', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'to', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'toto', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'tvuj', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'ty', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'u', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'v', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 've', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'vy', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'z', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'za', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('cs', 'ze', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('en', 'the', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('en', 'a', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('en', 'an', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('en', 'be', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('en', 'is', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('en', 'are', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('en', 'was', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('en', 'were', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('en', 'been', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('en', 'have', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('en', 'has', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('en', 'had', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('en', 'do', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('en', 'does', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('en', 'did', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('en', 'it', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('en', 'this', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('en', 'that', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('de', 'sein', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('de', 'ist', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('de', 'sind', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('de', 'war', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('de', 'waren', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('de', 'haben', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('de', 'hat', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('de', 'hatte', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('de', 'ein', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('de', 'eine', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('de', 'der', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('de', 'die', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('de', 'das', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('de', 'es', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('de', 'dies', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('es', 'ser', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('es', 'es', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('es', 'son', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('es', 'era', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('es', 'eran', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('es', 'estar', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('es', 'esta', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('es', 'tener', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('es', 'tengo', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('es', 'tiene', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('es', 'el', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('es', 'la', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('es', 'los', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('es', 'las', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('es', 'esto', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)'),
  ('es', 'eso', 'coverage', 'lifted from _COVERAGE_STOP (migration 60)')
ON CONFLICT DO NOTHING;

COMMIT;
