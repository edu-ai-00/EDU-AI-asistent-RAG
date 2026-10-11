-- last change in code by Jan Drozd (2026-09-03).
--
-- Migrace 204: llm_variants — poslední LLM volání bez konfigurace
--
-- generate_variants() (answer.py) vygeneruje na cestě no_answer návrhy, jak se
-- zeptat jinak, když je pool prázdný. Je to plnohodnotné LLM volání, ale žádný
-- řádek settings pro něj nikdy nic nenesl: jelo na defaultech zadrátovaných
-- v kódu, takže z konfigurace nebylo vidět, nedalo se vypnout a nespočítalo se
-- do latence ani do nákladů.
--
-- PROČ TEĎ, když ho 156 v seznamu "NOT added, with reasons" výslovně vynechala
-- s odůvodněním "a nested sub-config, not a scalar knob": 156 si zvolila obor
-- skalárních knobů a v tom oboru bylo to rozhodnutí správné. Vnořené objekty
-- v settings od té doby existují (rerank_prefilter, migrace 188) a s nimi padá
-- jediný důvod, proč tu llm_variants nebyly. Věcný argument 156 platí dál:
-- knob, který dnes nic nemění, je právě ten, který příště nikdo nenajde.
--
-- HODNOTY. enabled / max_tokens / temperature jsou přesně dnešní chování:
--   enabled      true  — kód má default True
--   max_tokens   200   — kódový default; NEDĚDÍ se z max_tokens kroku (2000),
--                        návrhy jsou čtyři řádky, ne odpověď
--   temperature  0     — dnes se dědí z kroku answer, kde 0 je
-- reasoning_effort JE změna a je to důvod, proč se to píše dohromady:
--   Volání dosud effort neposílalo vůbec, takže na gpt-5.6-luna běželo na
--   VLASTNÍM defaultu modelu, zatímco odpověď běží na medium. Teď je explicitní.
--   ZMĚŘENO 3. 9. 2026 na třech otázkách s prázdným poolem (ZS_KAVCI_HORY,
--   gpt-5.6-luna): bez effortu 2,5 / 1,5 / 1,8 s, medium 3,8 / 1,4 / 1,7 s, obojí
--   3–4 varianty. Rozdíl je tedy v pásmu šumu a NENÍ to důvod té změny — důvodem
--   je, že se hodnota přestává dědit z vendorova defaultu, který nikdo nevidí a
--   který se může změnit bez nás. Varování v llm.py ("default spends minutes")
--   je měřené na uzemněné odpovědi, ne na tomhle volání se 200 tokeny; na
--   návrhy se nepřeneslo.
--   Pozor: llm.py při neprázdném effortu temperature ZAHAZUJE (reasoning tiery
--   ji odmítají). Oba klíče tu jsou proto stejně jako na kroku answer, který
--   nese také obojí; temperature se uplatní, až když se effort vyprázdní na ""
--   — což je i způsob, jak se vrátit k chování před touto migrací.
--
-- POŘADÍ: reasoning_effort čte až kód s generate_variants() opravenou v této
-- dávce. Na starším image je ten klíč neškodně mrtvý — zapíše se, ale volání
-- jede dál na defaultu modelu, dokud se search nepřebuildí.
--
-- model tu SCHVÁLNĚ NENÍ: prázdná hodnota propadne na model kroku answer, což
-- je dnešní chování, a nevznikne druhé místo, kde se model mění. Přidat
-- "model": "…" má smysl teprve, když se návrhy mají odpojit na levnější model.

BEGIN;

UPDATE edu_ai.settings s
SET config = jsonb_set(s.config, '{pipeline}', (
  SELECT jsonb_agg(CASE WHEN st->>'step' = 'answer'
    THEN jsonb_set(st, '{params}', coalesce(st->'params','{}'::jsonb)
         || jsonb_build_object('llm_variants', jsonb_build_object(
              'enabled', true,
              'max_tokens', 200,
              'temperature', 0,
              'reasoning_effort', 'medium')), true)
    ELSE st END ORDER BY i)
  FROM jsonb_array_elements(s.config->'pipeline') WITH ORDINALITY t(st, i)), true)
WHERE s.is_active;

DO $$
DECLARE
    n int;
BEGIN
    -- settings jsou per organizace: kdyby to sedělo jen na globálním řádku,
    -- školy by běžely dál na kódových defaultech (AGENTS.md)
    SELECT count(*) INTO n
      FROM edu_ai.settings s, jsonb_array_elements(s.config->'pipeline') st
     WHERE s.is_active AND st->>'step' = 'answer'
       AND NOT (st->'params'->'llm_variants'
                ?& array['enabled','max_tokens','temperature','reasoning_effort']);
    IF n > 0 THEN
        RAISE EXCEPTION '204: llm_variants chybí nebo je neúplný na % řádcích', n;
    END IF;

    SELECT count(*) INTO n
      FROM edu_ai.settings s, jsonb_array_elements(s.config->'pipeline') st
     WHERE s.is_active AND st->>'step' = 'answer';
    RAISE NOTICE '204: llm_variants je vidět na % aktivních řádcích', n;
END $$;

COMMIT;
