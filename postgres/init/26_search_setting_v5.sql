-- last change in code by Martin Safranek (2026-07-24).
-- 26_search_setting_v5.sql
-- Calibration on top of v3: make the document-vector fallback reachable without
-- regressing real traffic. Derived from the active v3 row via jsonb_set so this
-- file only encodes the DELTA (and stays byte-identical to how v5 was created live):
--   • rag_keywords.min_overlap   1  -> 2   (pipeline[4]) — stop single-token keyword
--     hits from pre-empting the vector step
--   • rag_vector.cosine_min      0.65 -> 0.60 (pipeline[6]) — let genuine semantic
--     document matches through
--   • qa_vector.cosine_min       kept 0.75 — a lower gate returned topically-loose
--     curated answers VERBATIM (precision risk), so it stays strict.
-- Measured 2026-07-24: identical outcome/source distribution vs v3 on a 30-question
-- real eval sample — no regression; both vector RAG paths now demonstrably reachable.
--
-- Idempotent by label: on a fresh DB (01 seeds v1/v2/v3, v3 active) this inserts v5
-- and activates it; re-running (or applying on a server that already has v5) is a
-- no-op. Respects the one-active partial unique index by deactivating others first.
-- Transactional.

BEGIN;

INSERT INTO edu_ai.search_setting (setting_id, label, is_active, config, notes, created_at)
SELECT gen_random_uuid(),
       'v5: v3 + doc-vector reachable (rag_vec=0.60, kw_overlap=2; qa_vec kept 0.75)',
       false,
       jsonb_set(jsonb_set(
           config,
           '{pipeline,4,params,min_overlap}', '2',    true),
           '{pipeline,6,params,cosine_min}',  '0.60', true),
       'Calibration on top of v3: doc-vector gate 0.65->0.60 + keyword min_overlap 1->2 so rag_vector is reachable; qa_vector kept strict 0.75. Measured: no regression vs v3 on 30-q sample (2026-07-24).',
       now()
FROM edu_ai.search_setting
WHERE label LIKE 'v3:%'
  AND NOT EXISTS (SELECT 1 FROM edu_ai.search_setting WHERE label LIKE 'v5:%');

-- Flip active to v5 (deactivate everything else FIRST — one-active constraint).
UPDATE edu_ai.search_setting SET is_active = false WHERE is_active     AND label NOT LIKE 'v5:%';
UPDATE edu_ai.search_setting SET is_active = true  WHERE label LIKE 'v5:%' AND NOT is_active;

COMMIT;
