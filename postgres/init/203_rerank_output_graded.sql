-- Migrace 203: rerank_output = graded
--
-- Nález z obnovy 27. 8. 2026. Serverový export neobsahoval `rerank_output`, a
-- protože kód bez něj bere větev "order" (_rr_out = params.get(...) or "order"),
-- běžel by soudce v jiném režimu, než v jakém se dnes měřilo všechno kolem
-- rerankeru. To nastavení přitom nikdy nebylo v migraci — existovalo jen jako
-- ruční UPDATE, takže by se ztratilo při každé obnově.
--
-- PROČ graded. Listwise reranker vrací PERMUTACI a skóre z ní odvozené jsou
-- pozice v přestrojení: "6.0" znamená jedenáctý ze šestnácti, ne "slabý", takže
-- na ně nejde napsat absolutní práh a každý řez je hádání o velikosti poolu.
-- Známkovaný verdikt (2 / 1 / 0) je naproti tomu rozhodnutí o jedné pasáži a nese
-- veto. Změřeno 27. 8. na devíti otázkách s vypnutou QA: soudce 9/9, Jina jako
-- řadič 6/9 — a rozdíl dělalo právě to veto, ne pořadí.
--
-- rerank_grade_ordering zůstává false: verdikt tedy VETUJE a pořadí určují tiery
-- (ročník, autorita, nadpis). To je varianta měřená 23. 8.

UPDATE edu_ai.settings s
SET config = jsonb_set(s.config, '{pipeline}', (
  SELECT jsonb_agg(CASE WHEN st->>'step' = 'answer'
    THEN jsonb_set(st, '{params}', coalesce(st->'params','{}'::jsonb)
         || jsonb_build_object('rerank_output', 'graded'), true)
    ELSE st END ORDER BY i)
  FROM jsonb_array_elements(s.config->'pipeline') WITH ORDINALITY t(st, i)), true)
WHERE s.is_active;
