-- last change in code by Martin Safranek (2026-08-22).
--
-- Migration 166: model rerankeru do settings (a rovnou na gpt-5.6-luna).
--
-- Reranker měl konfiguraci na třech místech: režim v settings, prompt v
-- prompt_registry (migrace 165) a MODEL v docker-compose (RERANK_OPENAI_MODEL),
-- tedy jediný kus, který nešel změnit bez rebuildu. Právě on přitom vysvětluje
-- to, na co všechna dnešní měření ukazovala: odpověď píše gpt-5.6-luna, ale
-- soudce nad jejím kontextem byl gpt-4o-mini. Ten rozdával na dotaz přesně jednu
-- dvojku (1 z 8, 1 z 20, a znovu 1 z 20 i po přepsání promptu, kde se výslovně
-- říká, že stačí ČÁST odpovědi a dvojek může být kolik chce) a u „Jak požádat o
-- individuální vzdělávací plán?" vetoval čtyři z pěti chunků školního řádu, které
-- to slovo obsahují — a to je viděl, ověřeno na pozicích ve zkrácené pasáži.
--
--   rerank_model              prázdno = fallback na env; jinak jméno modelu
--   rerank_reasoning_effort   'low' — 5.x tier bez toho na jeden soud spotřebuje
--                             minuty, což se do 3sekundového vyhledávání nevejde
--                             (a temperature ty modely odmítají, takže se posílá
--                             reasoning_effort místo ní)
UPDATE edu_ai.settings s
   SET config = jsonb_set(s.config::jsonb, '{pipeline}', (
         SELECT jsonb_agg(
                  CASE WHEN e.elem ->> 'step' <> 'answer' THEN e.elem
                       ELSE jsonb_set(e.elem, '{params}',
                              CASE WHEN jsonb_typeof(e.elem -> 'params') = 'object'
                                   THEN e.elem -> 'params' ELSE '{}'::jsonb END
                              || jsonb_build_object(
                                   'rerank_model', 'gpt-5.6-luna',
                                   'rerank_reasoning_effort', 'low'))
                  END ORDER BY e.ord)
           FROM jsonb_array_elements(s.config::jsonb -> 'pipeline')
                WITH ORDINALITY AS e(elem, ord)
       ))::json
 WHERE s.is_active
   AND jsonb_typeof(s.config::jsonb -> 'pipeline') = 'array'
   AND jsonb_array_length(s.config::jsonb -> 'pipeline') > 0;
