-- last change in code by Martin Safranek (2026-09-04).
-- 209_qa_stopword_form.sql
-- qa_stopword.form — WHICH reduced column an entry applies to.
--
-- The table already says what an entry is FOR (scope: index, coverage,
-- section_word, meta_verb, context_anaphora, context_leading, bm25_grade,
-- meta_filler). It has never said WHERE the entry applies, and that is a second,
-- independent axis: a token is compared against the string a reduction produced,
-- and the three reductions produce three different strings for one word.
--
--   jsou   ->  norm `jsou`   lemma `byt`   stem `jso`
--   mám    ->  norm `mam`    lemma `mit`   stem `mam`
--   hlava  ->  norm `hlava`  lemma `hlava` stem `hlav`
--
-- One flat term therefore lands in whichever column happens to spell it the
-- same way and silently misses the others. Measured 2026-09-04 on the 4. 9.
-- production dump: `jsou` is in the list, `jso` is in no hand-written list, so
-- "jsou" survives in EVERY stem_text in the corpus. And `hlava` is a
-- section_word, which is why "V které hlavě školního řádu to je?" drops it from
-- the lemma column but keeps `hlav` in the stem column.
--
-- WHY NOT DERIVE IT instead. Tried on 2026-09-04 and reverted: reducing the
-- terms with the live reducer is unsafe for the stem column, because stemming is
-- lossy. The preposition `kolem` stems to `kol` and so does the content word
-- `kolo`, so a derived stem list deleted the subject of "2. kolo přijímaček".
-- The parity canary caught it. On the lemma column derivation was safe but
-- amplified a borderline entry: `sám` as a surface form is harmless, as a lemma
-- family it killed "Můžeme být na výletě chvíli sami?" and six more. Writing the
-- form out is the auditable alternative — one entry, one decision, measurable.
--
-- DEFAULT 'all' IS TODAY'S BEHAVIOUR, so this migration is inert: an entry with
-- form='all' is compared against every column exactly as before. No backfill,
-- no reindex, and nothing reads the column yet — deliberately. The readers
-- (settings.py plus the three ChunkLemmatizer components) must first share ONE
-- config loader; adding a second axis to four independent readers is how the
-- scope axis ended up applied on one side only (migration 60, fixed in a105002).
--
-- Idempotent; transactional.

BEGIN;

ALTER TABLE edu_ai.qa_stopword
    ADD COLUMN IF NOT EXISTS form text NOT NULL DEFAULT 'all';

ALTER TABLE edu_ai.qa_stopword
    DROP CONSTRAINT IF EXISTS qa_stopword_form_chk;
ALTER TABLE edu_ai.qa_stopword
    ADD CONSTRAINT qa_stopword_form_chk
    CHECK (form IN ('all', 'norm', 'lemma', 'stem'));

-- a term may need to exist for more than one form, so it joins the key
ALTER TABLE edu_ai.qa_stopword DROP CONSTRAINT IF EXISTS qa_stopword_pkey;
ALTER TABLE edu_ai.qa_stopword ADD PRIMARY KEY (lang, term, scope, form);

COMMENT ON COLUMN edu_ai.qa_stopword.form IS
  'Which reduced column this entry applies to: all (default, today''s behaviour — compared against every column), norm, lemma or stem. Independent of scope, which says what the entry is FOR. Write the term in the form it must match: to drop the lemma of "mám" the term is `mit`, not `mam`. Do NOT derive stem forms from other entries — stemming is lossy and a stopword root collides with content roots (kolem/kolo -> kol). See migration 209.';

COMMIT;
