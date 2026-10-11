-- last change in code by Martin Safranek (2026-08-23).
--
-- Migrace 176: soudce vetuje, pořadí drží stupně (rerank_reorder = false).
--
-- Naměřeno na třech identických bězích "Co se hodnotí u přijímaček"
-- (question_answer_log 5416/5417/5418): identický pool, identická fúzní skóre,
-- identické soudcovy pozice 1 a 2 — a od pozice 3 tři různá pořadí. Tím se
-- rozešel kontext: chunk "Hodnocení zkoušky" s pravidlem o 60 % se do odpovědi
-- dostal v JEDNOM běhu ze tří. Zbylé dvě odpovědi to pravidlo nezamlčely, ony
-- ho v kontextu neměly. Uživateli to vypadá, že "pokaždé vyhraje jiný dokument"
-- — a vypadá, protože citace se odvozuje z textu odstavce.
--
-- rerank_grade_ordering=false už říká "známka jen vetuje, nikdy nevede", ale
-- skóre `rerank` je pak soudcova POZICE a fáze 4 i 5 čtou pool v tomto pořadí,
-- takže soudce vedl dál — jen permutací místo známky. A permutace je volný
-- text z LLM, tedy to nejméně stabilní v celé kaskádě. rerank_reorder=false
-- vrací přeživší do tier pořadí (SQL + fúze, deterministické) a veto ponechává.
BEGIN;

UPDATE edu_ai.settings s
   SET config = jsonb_set(s.config, '{pipeline}', (
         SELECT jsonb_agg(
                  CASE WHEN st->>'step' = 'answer'
                       THEN jsonb_set(st, '{params}',
                              coalesce(st->'params', '{}'::jsonb)
                              || jsonb_build_object('rerank_reorder', false), true)
                       ELSE st END
                  ORDER BY i)
           FROM jsonb_array_elements(s.config->'pipeline') WITH ORDINALITY t(st, i)), true)
 WHERE s.is_active;

COMMIT;
