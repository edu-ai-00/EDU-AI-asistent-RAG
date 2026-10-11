-- last change in code by Martin Safranek (2026-08-27).
--
-- Migrace 187: reranker má třetí volbu — hostovaný cross-encoder (Jina).
--
-- Dosud dvě: `local` (bge v procesu) a `cloud` (jazykový model, listwise).
-- Cloudová varianta vrací PERMUTACI, a na pozici se nedá napsat práh — proto
-- muselo veto být verdikt (známka 2/1/0), který model při každém běhu rozhoduje
-- znovu. Cross-encoder vrací kalibrované skóre, takže veto je jedno číslo
-- v konfiguraci.
--
-- Naměřeno 27. 8. 2026 na dvaceti pasážích ze skutečného poolu:
--   latence      419–456 ms   (proti 3 000–8 000 ms, u effort=high až 16 000)
--   tokeny       5 392 vstup, výstup nula (LLM platí i reasoning na výstupu)
--   cena/dotaz   ~$0,0001     (proti ~$0,0011 + reasoning)
--   determinismus  tři identická volání → identické skóre na čtyři desetiny
-- A na poolu, který rozbil dotaz „můžeme ve škole fotit?": pravidlo o
-- fotografování 0,3224, tematicky blízká „Kavky Environmental Challenge" 0,0125,
-- zbytek negativní. Oddělení o řád — přesně to, co u permutace nešlo.
--
-- Parametry (per organizace, jako všechno ostatní v pipeline):
--   rerank_mode        off | local | cloud | jina
--   rerank_model       u jina = model API (jina-reranker-v3.5), u cloud = LLM
--   rerank_score_min   pasáž pod tímto skóre padá z poolu; 0 = jen řadí.
--                      Platí JEN pro skórovací backendy (local, jina).
--   rerank_timeout_s   strop na volání; při selhání se drží fúzní pořadí
--
-- Tato migrace režim NEPŘEPÍNÁ — nechává `cloud` a jen doplňuje parametry, aby
-- přepnutí bylo změna jedné hodnoty a šlo změřit proti dnešnímu základu.
-- Klíč JINA_API_KEY žije v edu_ai.env (neverzovaný) a compose ho předává jen
-- službě edu_ai_search, stejně jako OPENAI_API_KEY. Prázdný klíč není chyba:
-- rerank.py drží fúzní pořadí a hledání pokračuje.
BEGIN;

UPDATE edu_ai.settings s
   SET config = jsonb_set(s.config, '{pipeline}', (
         SELECT jsonb_agg(
                  CASE WHEN st->>'step' = 'answer'
                       THEN jsonb_set(st, '{params}',
                              coalesce(st->'params', '{}'::jsonb) || jsonb_build_object(
                                'rerank_score_min', 0,
                                'rerank_timeout_s', 15
                              ), true)
                       ELSE st END
                  ORDER BY i)
           FROM jsonb_array_elements(s.config->'pipeline') WITH ORDINALITY t(st, i)), true)
 WHERE s.is_active;

COMMENT ON COLUMN edu_ai.settings.config IS
    'Pipeline jako data. Krok answer: rerank_mode = off | local | cloud | jina '
    '(local = bge v procesu, cloud = LLM listwise s permutací, jina = hostovaný '
    'cross-encoder s kalibrovaným skóre). rerank_score_min vetuje pasáž pod '
    'daným skóre a platí jen pro skórovací backendy; u cloud tuto roli hraje '
    'rerank_require_grade. Viz migrace 187.';

COMMIT;
