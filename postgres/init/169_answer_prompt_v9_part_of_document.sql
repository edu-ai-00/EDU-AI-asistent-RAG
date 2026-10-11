-- last change in code by Martin Safranek (2026-08-23).
--
-- Migrace 169: ročníková patička mluví o ČÁSTI dokumentu, ne o celém dokumentu.
--
-- Věta „Text pochází z dokumentu, který se týká 5., 7. a 9. ročníku." tvrdí víc,
-- než je pravda: ročníky nesou CHUNKY, ne dokument. Školní řád ZŠ+SŠ v jednom PDF
-- má části pro 6. ročník i pro maturitní obory, kalendář akcí má řádek pro 2.
-- stupeň i pro 3.–5. třídu. Odpověď stojí na konkrétních pasážích, a jen o těch
-- ta patička může mluvit. Text v v8 už tu možnost připouštěl („a document (or a
-- part of a document)"), ale příklady mluvily o dokumentu, a model jde za
-- příkladem.
BEGIN;

-- unikátní index dovolí jen JEDNU aktivní verzi na prompt_key, takže se v8 musí
-- zhasnout PŘED vložením v9 — a celé to jde v jedné transakci, aby při chybě
-- nezůstal prompt bez aktivní verze.
UPDATE edu_ai.prompt_registry SET is_active = false
 WHERE prompt_key = 'search_answer_generation' AND version = 8
   AND EXISTS (SELECT 1 FROM edu_ai.prompt_registry p
                WHERE p.prompt_key = 'search_answer_generation' AND p.version = 8
                  AND p.is_active);

INSERT INTO edu_ai.prompt_registry
       (prompt_key, version, prompt_text, is_active, component, description,
        model_hint, lang, created_by, notes)
SELECT prompt_key, 9,
       replace(replace(replace(replace(replace(prompt_text,
         'stating that the text COMES FROM a document (or a part of a document) that' || chr(10) ||
         '   concerns that grade',
         'stating that the text COMES FROM a PART of a document that concerns that' || chr(10) ||
         '   grade'),
         'Text pochází z dokumentu, který se týká 6. ročníku.',
         'Text pochází z části dokumentu, která se týká 6. ročníku.'),
         'This text comes from a document concerning grade 6.',
         'This text comes from a part of a document concerning grade 6.'),
         'Text pochází z dokumentu, který se týká všech ročníků.',
         'Text pochází z části dokumentu, která se týká všech ročníků.'),
         'Text pochází z dokumentů, které se týkají',
         'Text pochází z částí dokumentů, které se týkají'),
       true, component, description, model_hint, lang, 'migration-169',
       'v9: ročníková patička mluví o ČÁSTI dokumentu — ročníky nese chunk, ne dokument.'
  FROM edu_ai.prompt_registry
 WHERE prompt_key = 'search_answer_generation' AND version = 8
   AND NOT EXISTS (SELECT 1 FROM edu_ai.prompt_registry p2
                    WHERE p2.prompt_key = 'search_answer_generation' AND p2.version = 9);

COMMIT;
