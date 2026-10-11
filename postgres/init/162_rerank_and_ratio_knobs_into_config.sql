-- last change in code by Martin Safranek (2026-08-22).
--
-- Migration 162: the reranker's and the citation bar's knobs become DATA.
--
-- Six values steered the answer step from inside the code (`params.get(...) or N`)
-- and could not be seen, let alone turned, from the settings row:
--
--   rerank_passage_chars   how much of a chunk the judge reads (500; the graded
--                          mode reads 900 because a veto on a third of a chunk is
--                          a different kind of mistake)
--   rerank_min_keep        floor under the graded veto — never veto into a hole
--   rerank_require_grade   which verdict lets a document seed a paragraph
--   rerank_seed_top_n      how deep in the reranked order a document may still
--                          seed one (measured: legitimate 2nd documents sat at
--                          positions 2-10, the "fotit" balast at 11-12)
--   source_ratio_anchor    WHOSE score the source_min_ratio bar is measured
--                          against — 'tier_first' (the tier order's top chunk,
--                          captured before the reranker reorders), 'pool_first'
--                          (the old behaviour) or 'pool_max'
--
-- and one step-level flag:
--
--   rag_keywords.only_if_empty = false — the keyword step used to run ONLY on an
--   empty pool, so on every answered question its opinion was never asked; it
--   annotates keyword_rank on chunks the other retrievers found, which the fusion
--   weights, so skipping it threw that signal away.
--
-- Every value equals the code's own default, so behaviour is unchanged — they are
-- written down so they can be SEEN and turned. Per-org: the row list is every
-- ACTIVE settings row, not just the global one (a knob added only globally is
-- invisible to a school that has its own row).
UPDATE edu_ai.settings s
   SET config = jsonb_set(s.config::jsonb, '{pipeline}', (
         SELECT jsonb_agg(
                  CASE WHEN e.elem ->> 'step' <> 'answer' THEN e.elem
                       ELSE jsonb_set(e.elem, '{params}',
                              CASE WHEN jsonb_typeof(e.elem -> 'params') = 'object'
                                   THEN e.elem -> 'params' ELSE '{}'::jsonb END
                              || jsonb_build_object(
                                   'rerank_passage_chars', 500,
                                   'rerank_min_keep', 6,
                                   'rerank_require_grade', 2,
                                   'rerank_seed_top_n', 12,
                                   'source_ratio_anchor', 'tier_first'))
                  END ORDER BY e.ord)
           FROM jsonb_array_elements(s.config::jsonb -> 'pipeline')
                WITH ORDINALITY AS e(elem, ord)
       ))::json
 WHERE s.is_active
   AND jsonb_typeof(s.config::jsonb -> 'pipeline') = 'array'
   AND jsonb_array_length(s.config::jsonb -> 'pipeline') > 0;

UPDATE edu_ai.settings s
   SET config = jsonb_set(s.config::jsonb, '{pipeline}', (
         SELECT jsonb_agg(
                  CASE WHEN e.elem ->> 'step' <> 'rag_keywords' THEN e.elem
                       ELSE jsonb_set(e.elem, '{only_if_empty}', 'false'::jsonb)
                  END ORDER BY e.ord)
           FROM jsonb_array_elements(s.config::jsonb -> 'pipeline')
                WITH ORDINALITY AS e(elem, ord)
       ))::json
 WHERE s.is_active
   AND jsonb_typeof(s.config::jsonb -> 'pipeline') = 'array'
   AND jsonb_array_length(s.config::jsonb -> 'pipeline') > 0;

-- Doplněk 22. 8. večer: rerank_grade_ordering. Řídí, co všechno smí verdikt
-- rozhodnout. true (dosavadní chování) = skóre je verdict-major, celý blok
-- grade 1 leží pod blokem grade 2. false = skóre je čistě pozice jako v režimu
-- order a verdikt rozhoduje JEN o vyhození nul. Měřeno: gpt-4o-mini dá na dotaz
-- přesně jednu dvojku, ať prompt říká cokoli (1 z 8, 1 z 20, a znovu 1 z 20 i po
-- přepsání promptu) — řadit podle toho znamená jeden chunk nahoře a zbytek na
-- jedné hromadě. Nuly ale rozdává správně, u „fotit" patnáct ze šestnácti.
UPDATE edu_ai.settings s
   SET config = jsonb_set(s.config::jsonb, '{pipeline}', (
         SELECT jsonb_agg(
                  CASE WHEN e.elem ->> 'step' <> 'answer' THEN e.elem
                       ELSE jsonb_set(e.elem, '{params}',
                              CASE WHEN jsonb_typeof(e.elem -> 'params') = 'object'
                                   THEN e.elem -> 'params' ELSE '{}'::jsonb END
                              || jsonb_build_object('rerank_grade_ordering', true))
                  END ORDER BY e.ord)
           FROM jsonb_array_elements(s.config::jsonb -> 'pipeline')
                WITH ORDINALITY AS e(elem, ord)
       ))::json
 WHERE s.is_active
   AND jsonb_typeof(s.config::jsonb -> 'pipeline') = 'array'
   AND jsonb_array_length(s.config::jsonb -> 'pipeline') > 0;
