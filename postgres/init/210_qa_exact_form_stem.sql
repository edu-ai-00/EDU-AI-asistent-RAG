-- last change in code by Martin Safranek (2026-09-05).
-- 210_qa_exact_form_stem.sql
-- qa_exact: vypsat `form` a zapnout třetí tvar (stem).
--
-- DVĚ VĚCI NARÁZ, obě záměrné.
--
-- 1) VYPSAT DEFAULT. Krok qa_exact dosud neměl `params` vůbec, takže z
--    konfigurace nešlo poznat, proti čemu porovnává — tiše platil
--    _DEFAULT_FORM = ["norm","lemma"] z search/app/nodes/qa_common.py:21.
--    Parametr, který v settings chybí, vypadá, že se ho ta věc netýká;
--    přitom rozhoduje. Totéž se stalo u llm_variants (migrace 204).
--
-- 2) PŘIDAT stem. qa_exact porovnává ROVNOSTÍ celého redukovaného řetězce,
--    OR přes zvolené tvary (qa_exact.py:29-35), takže třetí tvar znamená
--    jednu podmínku navíc:
--       WHERE ... AND (v.norm_text = %s OR v.lemma_text = %s OR v.stem_text = %s)
--
-- CO TO PODLE MĚŘENÍ UDĚLÁ. Na 482 unikátních reálných dotazech z
-- question_answer_log (4. 9. 2026): 260 už dnes trefí qa_exact přes
-- norm|lemma, přes stem by přibyl PRÁVĚ JEDEN — "kdo resi neomluvenu
-- absenci", tedy překlep ve skloňování, kde kořen `neomluven` sedne tam, kde
-- `neomluvenu` ne. Ten dotaz dnes stejně zodpoví qa_vector se skóre 0.9759.
--
-- RIZIKO KOLIZE JE ZMĚŘENÉ A MALÉ. Mezi 2092 aktivními variantami je 7 skupin,
-- kde různá lemma sdílejí týž stem_text — a všech 7 jsou DIAKRITICKÉ DVOJICE
-- téže otázky ("kdy zacina skolni rok" vs "Kdy začíná školní rok?"), které
-- packy obsahují záměrně. Shoda na kořeni je u nich správná.
--
-- CO ZŮSTÁVÁ NÁMITKOU. qa_exact vrací answer_md doslova, se skóre 1.0, a
-- ukončí kaskádu; jeho hodnota je v tom, že si je JISTÝ. Shoda kořenů jistota
-- není — stemmer zahazuje koncovky, a ty v češtině nesou pád. Případ, který
-- dnes prochází o krok dál přes práh a margin v qa_lexical, se tím přesune do
-- kategorie "bez pochybností". Vracet se to dá jedním UPDATE zpět na
-- ["norm","lemma"].
--
-- Píše se do KAŽDÉHO aktivního řádku settings, ne jen do platformního —
-- settings jsou per organizace (AGENTS.md, migrace 98).
--
-- Idempotentní; transakční.

BEGIN;

-- POZOR na tvar zápisu: krok qa_exact nemá klíč `params` VŮBEC (ne prázdný
-- objekt — klíč chybí), a jsonb_set s create_missing vytvoří jen POSLEDNÍ
-- prvek cesty, ne chybějícího rodiče. Cesta ['params','form'] proto tiše
-- neudělá nic a UPDATE ohlásí úspěch. Zapisuje se tedy celý objekt `params`,
-- složený z toho, co tam je (nebo prázdna), sloučením s novým klíčem — tak to
-- funguje i pro řádek, který params už má, a nepřepíše mu ostatní klíče.

UPDATE edu_ai.settings s
   SET config = jsonb_set(
         s.config,
         ARRAY['pipeline', (m.idx - 1)::text, 'params'],
         coalesce(m.step->'params', '{}'::jsonb)
           || '{"form": ["norm", "lemma", "stem"]}'::jsonb,
         true)
  FROM (SELECT x.setting_id, t.ordinality AS idx, t.step
          FROM edu_ai.settings x,
               jsonb_array_elements(x.config->'pipeline')
               WITH ORDINALITY AS t(step, ordinality)
         WHERE x.is_active AND t.step->>'step' = 'qa_exact') AS m
 WHERE s.setting_id = m.setting_id AND s.is_active;

COMMIT;
