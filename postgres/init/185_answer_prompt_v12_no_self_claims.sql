-- last change in code by Martin Safranek (2026-08-26).
--
-- Migrace 185: prompt odpovědi v12 — text o tom, co umí ŽÁK, není výpovědí
-- o asistentovi.
--
-- Naměřeno 26. 8. 2026: na dotaz „co umíš spočítat?" přišla odpověď „Umím
-- spočítat matematické příklady, ale nemusím vždy vysvětlit postup řešení" a na
-- „co ještě umíš?" (question_answer_log 2897) věta „Umíš z textu získat
-- informace, zpracovat je a text zhodnotit". Není to halucinace — je to DOSLOVNÝ
-- text školního kurikula, které je psané ve druhé osobě o žákovi, jen vztažený na
-- špatný subjekt. Model převzal hlas dokumentu.
--
-- Routing to řeší v bráně (migrace 183 + doménový test v route.py): krátký dotaz
-- se schopnostním slovesem a bez školního tématu dostane meta odpověď. Tohle
-- pravidlo je POJISTKA pro dotazy, které branou projdou — delší formulace, cizí
-- jazyk bez vyplněných číselníků, nebo dotaz, kde školní téma je i není zároveň.
--
-- Umístěno do pravidla 5 (SUBJECT CHECK), protože je to tentýž mechanismus:
-- pravidlo 5 určuje PŘEDMĚT dotazu a používá jen pasáže, které o něm mluví. Když
-- je předmětem asistent sám, žádná pasáž o něm nemluví — ani ta, která používá
-- stejná slova. Nové číslo pravidla by znamenalo přečíslovat 6 a 7.
BEGIN;

UPDATE edu_ai.prompt_registry SET is_active = false
 WHERE prompt_key = 'search_answer_generation' AND version = 11 AND is_active;

INSERT INTO edu_ai.prompt_registry
       (prompt_key, version, prompt_text, is_active, component, description,
        model_hint, lang, created_by, notes)
SELECT prompt_key, 12,
       replace(prompt_text,
'   at all does rule 4 apply, and then its protocol is the ENTIRE output —
   never an answer with NO_ANSWER appended to it.',
'   at all does rule 4 apply, and then its protocol is the ENTIRE output —
   never an answer with NO_ANSWER appended to it.
   WHEN THE SUBJECT IS YOU: a question about what YOU can do, know, calculate,
   translate or help with is about the assistant, and NO passage speaks about
   that subject — not even one using the same words. School curricula are
   written in the SECOND PERSON ABOUT THE PUPIL ("umí z textu získat
   informace", "zvládne spočítat příklady"); such a sentence states what a
   pupil is expected to do and is never evidence about your abilities.
   Measured: "co umíš spočítat?" was answered "Umím spočítat matematické
   příklady" straight out of a curriculum. Never write a first-person claim
   about your own abilities, and never adopt the document''s voice as your own.
   Answer instead with one sentence: you answer questions from this school''s
   documents. This is the ONE case where the answer does not come from the
   passages.'),
       true, component,
       'v12: text o schopnostech žáka není výpovědí o asistentovi',
       model_hint, lang, 'martin',
       'Migrace 185 — measured on log 2897 and on "co umíš spočítat?"'
  FROM edu_ai.prompt_registry
 WHERE prompt_key = 'search_answer_generation' AND version = 11;

COMMIT;
