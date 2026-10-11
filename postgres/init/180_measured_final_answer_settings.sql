-- last change in code by Martin Safranek (2026-08-23).
--
-- Migrace 180: doměřený cílový stav kroku answer. Přebíjí 176 a 177.
--
-- Migrace 176 (rerank_reorder=false) a 177 (rerank_grade_ordering=true) zavedly
-- varianty A a B — soudce jen vetuje, resp. vetuje a třídí známkou. Obě byly
-- proměřeny na plné sadě 3x 42 otázek a obě vyšly HORŠÍ než původní režim doplněný
-- o opravy soudce:
--
--   baseline 19:31    39 odpovědí, 11 kolísavých
--   A (176)           36 odpovědí,  4 kolísavé   -- stabilita za cenu tří odpovědí
--   B (176+177)       38 odpovědí,  6 kolísavých
--   opravy + medium   39 odpovědí,  5 kolísavých, median 8,2 s, max 14,1 s
--   opravy + high     39 odpovědí,  5 kolísavých, median 9,3 s, max 22,7 s
--
-- Rozhodnutí: soudce zůstává v původním režimu (řadí i vetuje) a stabilitu
-- přinesly opravy v kódu (nesouzená pasáž = známka 0, značka o zkrácení, kontrola
-- permutace) plus prompt v3. Ve stavu "opravy + medium" se navíc ŽÁDNÁ otázka
-- nepřeklápí mezi odpovědí a mlčením; těch 5 kolísavých se liší jen počtem
-- citovaných zdrojů.
--
-- reasoning_effort odpovědního modelu = medium: high nepřinesl ani jednu odpověď
-- navíc a nejpomalejší dotaz v něm trvá 22,7 s místo 14,1 s, což je za hranicí
-- použitelnosti v chatu.
--
-- Bez této migrace by čisté nasazení ze skriptů skončilo ve variantě B.
BEGIN;

UPDATE edu_ai.settings s
   SET config = jsonb_set(s.config, '{pipeline}', (
         SELECT jsonb_agg(
                  CASE WHEN st->>'step' = 'answer'
                       THEN jsonb_set(st, '{params}',
                              coalesce(st->'params', '{}'::jsonb) || jsonb_build_object(
                                'reasoning_effort',       'medium',
                                'rerank_reorder',         true,
                                'rerank_grade_ordering',  false,
                                'rerank_log_passages',    false
                              ), true)
                       ELSE st END
                  ORDER BY i)
           FROM jsonb_array_elements(s.config->'pipeline') WITH ORDINALITY t(st, i)), true)
 WHERE s.is_active;

COMMIT;
