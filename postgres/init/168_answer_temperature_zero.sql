-- last change in code by Martin Safranek (2026-08-22).
--
-- Migrace 168: temperature modelu odpovědi z 0,1 na 0.
--
-- Kolísání odpovědí bylo dnes hlavní stížnost při testování: dva stisky téhož
-- dotazu daly jinou odpověď. U „Kdo je ochránce osobních údajů a kde najdu GDPR
-- pravidla?" jsem dva běhy sedm sekund po sobě porovnal krok za krokem — pool 12
-- chunků, týchž 7 pasáží v kontextu, tytéž rerank pozice — a jeden odpověděl,
-- druhý mlčel. Retrieval je deterministický, láme se kompozice.
--
-- Měřeno na regresní sadě (3 běhy na otázku, většina):
--   temperature 0,1   33 odpovědí / 5, kolísá 8 případů, median RAG 4,7 s
--   temperature 0     33 odpovědí / 5, kolísá 5 případů, median RAG 4,1 s
-- Tři případy (N09, N11, N17) se srovnaly, žádná odpověď se neztratila. Nula
-- determinismus nezaručuje — zbylých pět případů kolísá dál, protože jejich vstup
-- je hraniční — ale je to nejlevnější dostupné zúžení rozptylu.
UPDATE edu_ai.settings s
   SET config = jsonb_set(s.config::jsonb, '{pipeline}', (
         SELECT jsonb_agg(
                  CASE WHEN e.elem ->> 'step' <> 'answer' THEN e.elem
                       ELSE jsonb_set(e.elem, '{params,temperature}', '0'::jsonb)
                  END ORDER BY e.ord)
           FROM jsonb_array_elements(s.config::jsonb -> 'pipeline')
                WITH ORDINALITY AS e(elem, ord)
       ))::json
 WHERE s.is_active
   AND jsonb_typeof(s.config::jsonb -> 'pipeline') = 'array'
   AND jsonb_array_length(s.config::jsonb -> 'pipeline') > 0;
