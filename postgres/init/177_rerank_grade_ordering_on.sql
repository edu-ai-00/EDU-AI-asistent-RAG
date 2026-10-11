-- last change in code by Martin Safranek (2026-08-23).
--
-- Migrace 177: známka soudce určuje třídu, stupně pořadí uvnitř třídy.
--
-- Doplněk k migraci 176. Samotné rerank_reorder=false (soudce jen vetuje) v evalu
-- 3× 45 otázek stabilitu zvedlo — kolísavých případů 4 místo 12 — ale zabilo čtyři
-- odpovědi. Doloženo na "kdy je lyžařský výcvik?" (question_answer_log 5491): fúze
-- dala 0,530 chunku "Zdroje informací" a 0,509 školnímu řádu, oba s dotazem
-- nesouvisí, a kalendářní řádek s LVK až 0,500. Soudce byl JEDINÝ krok, který to
-- viděl správně — jedinou dvojku dal kalendáři. Bez pořadí založil odstavec první
-- chunk a odpověď zmizela; vetovat nešlo, protože rerank_min_keep=6 drží všechny,
-- když jsou v poolu tři.
--
-- Známka je 2/1/0 a mezi běhy se reprodukuje; permutace dvaceti pasáží ne. Proto
-- se zapíná rerank_grade_ordering: třídu určí verdikt, pořadí uvnitř třídy stupně.
BEGIN;

UPDATE edu_ai.settings s
   SET config = jsonb_set(s.config, '{pipeline}', (
         SELECT jsonb_agg(
                  CASE WHEN st->>'step' = 'answer'
                       THEN jsonb_set(st, '{params}',
                              coalesce(st->'params', '{}'::jsonb)
                              || jsonb_build_object('rerank_grade_ordering', true), true)
                       ELSE st END
                  ORDER BY i)
           FROM jsonb_array_elements(s.config->'pipeline') WITH ORDINALITY t(st, i)), true)
 WHERE s.is_active;

COMMIT;
