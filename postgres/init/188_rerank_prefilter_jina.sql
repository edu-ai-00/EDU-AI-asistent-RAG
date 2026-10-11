-- 188: Jina jako PREFILTR před LLM soudce (ne jako řadič)
--
-- Dnes rozhoduje o tom, kdo se dostane k soudci, poziční řez: prvních
-- rerank_top_k chunků v tierovém pořadí, proložených podle ročníku. Změřeno
-- 27. 8. 2026 na 514 zalogovaných dotazech: median poolu 15, p90 42, max 75 —
-- proti rerank_top_k = 20. Na třetině dotazů tedy 20+ chunků nikdo neposoudil.
--
-- Prefiltr oskóruje celý pool hostovaným cross-encoderem a vybere slice pro
-- soudce. NIKDY nemění pořadí: jako řadič dal jina-reranker-v3.5 novoroční
-- přání nad kalendář akcí a focus gate pak vyhodil odpovídající dokument.
-- Jako prefiltr ten samý model odpovídající chunk zachoval (pozice 4 z 8).
--
-- Model je v3, ne v3.5: na dotazech M5 a M6 v3 odpovídá a v3.5 mlčí, a v3 dává
-- nerelevantním pasážím NEGATIVNÍ skóre, takže raw_min = 0 má smysl.
--
--   top_n         kolik pasáží dostane soudce (bylo 20 pozičně)
--   max_in        strop na jedno volání API — účtuje se za tokeny
--   raw_min       absolutní práh; jen na kalibrovaném modelu (v3), null = vypnuto
--   keep_anchor   tierová jednička projde vždy, ať si cizí model myslí co chce
--   passage_chars kolik znaků pasáže se posílá
--
-- Prefiltr se NEVOLÁ, když je pool <= rerank_top_k (soudce by viděl vše).

UPDATE edu_ai.settings s
SET config = jsonb_set(s.config, '{pipeline}', (
  SELECT jsonb_agg(CASE WHEN st->>'step' = 'answer'
    THEN jsonb_set(st, '{params}', coalesce(st->'params','{}'::jsonb)
         || jsonb_build_object('rerank_prefilter', jsonb_build_object(
              'backend',       'jina',
              'model',         'jina-reranker-v3',
              'top_n',         12,
              'max_in',        40,
              'raw_min',       0.0,
              'keep_anchor',   true,
              'passage_chars', 400,
              'timeout_s',     10)), true)
    ELSE st END ORDER BY i)
  FROM jsonb_array_elements(s.config->'pipeline') WITH ORDINALITY t(st, i)), true)
WHERE s.is_active;
