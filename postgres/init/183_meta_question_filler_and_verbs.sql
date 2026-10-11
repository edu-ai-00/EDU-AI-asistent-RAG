-- last change in code by Martin Safranek (2026-08-26).
--
-- Migrace 183: číselníky pro rozpoznání otázky o asistentovi.
--
-- Detekce meta otázky je striktní rovnost složeného dotazu s frází v
-- qa_blocklist (category='meta'). Ta striktnost je záměr — „co umíš spočítat"
-- je skutečný dotaz, ne meta. Znamená to ale, že každá zdvořilá či zdůrazňovací
-- varianta potřebuje vlastní řádek, a to je hra na krtky.
--
-- Naměřeno 26. 8. 2026 (question_answer_log 2897): „co ještě umíš?" se složí na
-- `co jeste umis`, v seznamu není, propadne do vyhledávání — a školní korpus na
-- to odpoví, protože ŠVP je psané ve DRUHÉ OSOBĚ O ŽÁKOVI. Odpověď zněla „Umíš
-- z textu získat informace, zpracovat je a text zhodnotit" a u dotazu „co umíš
-- spočítat?" dokonce „Umím spočítat matematické příklady". Není to halucinace,
-- je to doslovný text dokumentu vztažený na špatný subjekt.
--
-- Dva nové scope v qa_stopword:
--   meta_filler  slova, která nemění, na co se dotaz ptá; zahodí se PŘED
--                porovnáním, takže rovnost zůstává striktní
--   meta_verb    schopnostní slovesa ve druhé osobě; když po odečtení výplní a
--                tázacích slov nezbyde nic jiného, není v dotazu co dohledat
--                a odpovídá se meta odpovědí
--
-- Plus tři fráze, které v seznamu chyběly a nejsou pokryté výplněmi.
--
-- Oba číselníky jsou PO JAZYCÍCH (sloupec lang) a čtou se pro jazyk dotazu, takže
-- nový jazyk se zavede řádky, ne kódem. Totéž platí pro tázací slova, která
-- rozhodnutí používá: berou se z qa_synonym (kind='interrogative'), kde je 61
-- skupin ve čtyřech jazycích.
BEGIN;

INSERT INTO edu_ai.qa_stopword (scope, term, lang, is_active, note)
SELECT 'meta_filler', t, 'cs', true, 'Migrace 183 — nemění, na co se dotaz ptá'
  FROM unnest(ARRAY['jeste','vsechno','vse','tak','prosim','vlastne','uz',
                    'jinak','te','ti','mi','mne','nam']) AS t
 WHERE NOT EXISTS (SELECT 1 FROM edu_ai.qa_stopword s
                    WHERE s.scope='meta_filler' AND s.term=t AND s.lang='cs');

INSERT INTO edu_ai.qa_stopword (scope, term, lang, is_active, note)
SELECT 'meta_verb', t, 'cs', true, 'Migrace 183 — schopnostní sloveso ve 2. osobě'
  FROM unnest(ARRAY['umis','umite','dokazes','dokazete','zvladnes','zvladnete',
                    'pomuzes','pomuzete','delas','delate','jsi','jste',
                    'fungujes','fungujete','nabizis','nabizite']) AS t
 WHERE NOT EXISTS (SELECT 1 FROM edu_ai.qa_stopword s
                    WHERE s.scope='meta_verb' AND s.term=t AND s.lang='cs');

INSERT INTO edu_ai.qa_blocklist (category, term, term_norm, lang, is_active, source)
SELECT 'meta', t, t, 'cs', true, 'Migrace 183 — chybějící tvar'
  FROM unnest(ARRAY['na co se muzu zeptat','co umis delat','s cim mi muzes pomoct'])
       AS t
 WHERE NOT EXISTS (SELECT 1 FROM edu_ai.qa_blocklist b
                    WHERE b.category='meta' AND b.term_norm=t AND b.lang='cs');

COMMIT;
