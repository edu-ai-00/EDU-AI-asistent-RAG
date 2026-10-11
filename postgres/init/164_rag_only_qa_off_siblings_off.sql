-- last change in code by Martin Safranek (2026-08-22).
--
-- Migration 164: čistá RAG kaskáda — QA kroky a document_siblings vypnuté.
--
-- POZOR, tohle mění chování, ne jen viditelnost parametru:
--
--   qa_exact, qa_lexical, qa_vector = false
--     Kurátorované Q&A páry (tabulky qa_*) přestávají odpovídat. Každý dotaz jde
--     dokumentovou cestou (document_lexical → rag_keywords → rag_vector → answer).
--     Důvod: RAG se nedá vyhodnotit, dokud před ním stojí QA — na části otázek
--     odpoví QA a dokumentová cesta se vůbec nespustí, takže její chyby zůstanou
--     neviditelné. Martin to potřebuje takto, aby kolega pro infrastrukturu mohl RAG důkladně otestovat.
--     Zapnout zpět = 'enabled' na true u těch tří kroků (guard v route zůstává
--     zapnutý pořád, qa_blocklist tím není dotčen).
--
--   document_siblings = false (dlouhodobě, ne experiment)
--     Krok dotahoval k nalezenému chunku zbytek jeho sekce jako kontext. Měřeno
--     na regresní sadě: bez něj se nezměnilo nic (žádná odpověď, žádný zdroj), a
--     mezitím tu práci dělá fáze 5 v kroku answer — ta z dokumentu doplní chunky,
--     které retrievery skutečně našly, místo aby přidávala sousedy bez skóre.
--     Zůstává vypnutý; kdo ho bude chtít vrátit, ať k tomu má měření.
UPDATE edu_ai.settings s
   SET config = jsonb_set(s.config::jsonb, '{pipeline}', (
         SELECT jsonb_agg(
                  CASE WHEN e.elem ->> 'step' IN ('qa_exact', 'qa_lexical',
                                                  'qa_vector', 'document_siblings')
                       THEN jsonb_set(e.elem, '{enabled}', 'false'::jsonb)
                       ELSE e.elem
                  END ORDER BY e.ord)
           FROM jsonb_array_elements(s.config::jsonb -> 'pipeline')
                WITH ORDINALITY AS e(elem, ord)
       ))::json
 WHERE s.is_active
   AND jsonb_typeof(s.config::jsonb -> 'pipeline') = 'array'
   AND jsonb_array_length(s.config::jsonb -> 'pipeline') > 0;
