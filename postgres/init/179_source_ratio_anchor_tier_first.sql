-- last change in code by Martin Safranek (2026-08-23).
--
-- Migrace 179: kotva relativního prahu na tier_first, ne pool_first.
--
-- Odpověď smí citovat až sources_top_n dokumentů, ale druhý a další musí být
-- aspoň source_min_ratio (0,5) toho nejlepšího. Otázka je, od KOHO se ta polovina
-- počítá. pool_first bere první chunk AKTUÁLNÍHO poolu — tedy ten, který nahoru
-- vytáhl soudce, ne ten, který stupně vyhodnotily nejlépe. Soudce přitom často
-- promuje chunk se slabým skóre, a práh pak spadne tak nízko, že brána nefiltruje
-- nic.
--
-- Doloženo (komentář u answer.py, měření na "Jak škola předchází šikaně?"):
-- nejlepší chunk poolu měl topical 0,396, soudce dal na první místo 0,153, práh
-- tedy vyšel 0,077 místo 0,198. tier_first je proto default v kódu; nastavení ho
-- přebíjelo hodnotou, kterou tentýž kód dokumentuje jako rozbitou.
--
-- Vysvětluje to i pokles citovaných zdrojů 46 -> 33 mezi baseline a variantou A
-- (eval 3x 42 otázek, 23. 8.): varianta A na source_min_ratio nesáhla, jen vzala
-- soudci pořadí — čímž se kotva stala tím, čím měla být, a práh se vrátil.
BEGIN;

UPDATE edu_ai.settings s
   SET config = jsonb_set(s.config, '{pipeline}', (
         SELECT jsonb_agg(
                  CASE WHEN st->>'step' = 'answer'
                       THEN jsonb_set(st, '{params}',
                              coalesce(st->'params', '{}'::jsonb)
                              || jsonb_build_object('source_ratio_anchor', 'tier_first'), true)
                       ELSE st END
                  ORDER BY i)
           FROM jsonb_array_elements(s.config->'pipeline') WITH ORDINALITY t(st, i)), true)
 WHERE s.is_active;

COMMIT;
