-- 189: prefiltr rozhoduje KTERÝCH 20, ne KOLIK — absolutní práh pryč
--
-- Migrace 188 nasadila prefiltr s top_n = 12 a raw_min = 0. Změřeno 27. 8. 2026
-- na 42 otázkách × 3 s VYPNUTOU QA (se zapnutou QA dojde na odpovědní krok jen
-- 33 ze 126 dotazů, takže se prefiltr dotkne dvou otázek a měření nic neříká):
--
--                        odpovědí   změn vs baseline   M4      N10           N11
--   bez prefiltru        40/42      —                  —       —             —
--   188 (top_n 12, práh) 40/42      4                  lepší   ZKRÁCENO      bez termínu
--   189 (top_n 20)       40/42      3                  lepší   v pořádku     v pořádku
--
-- Příčina regresů: pasáže eduinského Průvodce jsou psané konverzačně („Do kdy
-- musíš podat přihlášku a jak“). Naše fúze je díky klíčovým slovům řadí vysoko,
-- ale cross-encoder je hodnotí níž než krátkou formální větu z CERMATu — a práh
-- je vyhodil, ještě než je soudce uviděl (median: ze 40 poslaných zůstalo 7).
--
-- Bez prahu prefiltr jen vybírá, KTERÝCH 20 pasáží soudce dostane. Původní díra
-- (pooly 50–75, kde 30+ chunků neviděl nikdo) se zalepí, soudci se nic neubere.
-- Tokeny neušetří — platí se 1,7 s na medianu. min_keep podlaha z 188 zůstává
-- v kódu jako pojistka pro případ, že by práh někdo znovu zapnul.

UPDATE edu_ai.settings s
SET config = jsonb_set(s.config, '{pipeline}', (
  SELECT jsonb_agg(CASE WHEN st->>'step' = 'answer'
    THEN jsonb_set(st, '{params}', coalesce(st->'params','{}'::jsonb)
         || jsonb_build_object('rerank_prefilter', jsonb_build_object(
              'backend',       'jina',
              'model',         'jina-reranker-v3',
              'top_n',         20,
              'max_in',        40,
              'keep_anchor',   true,
              'passage_chars', 400,
              'timeout_s',     10)), true)
    ELSE st END ORDER BY i)
  FROM jsonb_array_elements(s.config->'pipeline') WITH ORDINALITY t(st, i)), true)
WHERE s.is_active;
