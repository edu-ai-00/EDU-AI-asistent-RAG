-- Migrace 190: ročníková patička musí říct ročník POUŽITÉ pasáže, ne dokumentu.
--
-- Změřeno 27. 8. 2026: šesťák se zeptá „kdy je lyžařský výcvik?“, odpoví řádek
-- kalendáře označený {7,8} (síť pro jiné ročníky), a odpověď je věcně správná —
-- „pro 6. ročník termín není, lyžařský výcvik je pro 7. a 8. ročník 16.–20. 3.
-- 2026“. Patička pod ní ale tvrdila *Text pochází z části dokumentu, která se
-- týká 6. ročníku.* Šestku vzala od SESTERSKÉ pasáže téhož kalendáře (adaptační
-- kurz, {6,7}), protože patička je per dokument a ročníkovou čočkou projde jen
-- ročník tazatele.
--
-- Kód teď posílá druhý řádek metadat („Passages that belong to ANOTHER ročník“)
-- se skutečnými ročníky té pasáže. Prompt o něm ale nevěděl, takže ho model do
-- patičky nevzal — pravidlo 1 zná jedině řádek „Grades the context passages
-- apply to“. Tady se dozví o obou.
BEGIN;

UPDATE edu_ai.prompt_registry SET is_active = false
 WHERE prompt_key = 'search_answer_generation' AND version = 13 AND is_active;

INSERT INTO edu_ai.prompt_registry
       (prompt_key, version, prompt_text, is_active, component, description,
        model_hint, lang, created_by, notes)
SELECT prompt_key, 14,
       replace(prompt_text,
'   E.g. *Text pochází z části dokumentu, která se týká 6. ročníku.* /
   *This text comes from a part of a document concerning grade 6.* "9999" means every
   grade: *Text pochází z části dokumentu, která se týká všech ročníků.*',
'   E.g. *Text pochází z části dokumentu, která se týká 6. ročníku.* /
   *This text comes from a part of a document concerning grade 6.* "9999" means every
   grade: *Text pochází z části dokumentu, která se týká všech ročníků.*
   1b. When the input carries the line "Passages that belong to ANOTHER ročník
   than the caller''s" AND you used such a passage, the italic line MUST name
   THOSE ročníky — the ones of the passage you actually used — never the
   caller''s. Those two sets differ on purpose: the caller''s ročník can be in
   the document''s other parts, which is exactly what must not be claimed here.
   E.g. *Text pochází z části dokumentu, která se týká 7. a 8. ročníku.* When
   you used passages from both classes, name both sets in one line, e.g.
   *Text pochází z částí dokumentu, které se týkají 6. ročníku a 7. a 8.
   ročníku.*'),
       true, component,
       'v14: ročníková patička bere ročníky použité pasáže, ne dokumentu (jiný ročník)',
       model_hint, lang, 'martin', notes
  FROM edu_ai.prompt_registry
 WHERE prompt_key = 'search_answer_generation' AND version = 13;

-- pojistka: replace() musel něco udělat
DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n FROM edu_ai.prompt_registry
   WHERE prompt_key = 'search_answer_generation' AND version = 14
     AND prompt_text LIKE '%ANOTHER ročník%';
  IF n <> 1 THEN
    RAISE EXCEPTION 'migrace 190: kotva v promptu v13 nenalezena, v14 nevznikla';
  END IF;
END $$;

COMMIT;
