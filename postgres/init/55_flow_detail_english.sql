-- last change in code by Martin Safranek (2026-08-01).
-- 55_flow_detail_english.sql
-- The cascade trace's "step result" column is ALWAYS English, whatever the
-- interface language, so its wording moves into the page and these keys go.
--
-- Why English-only: the column's vocabulary is technical and already English on
-- both ends — the steps emit decision=A / outcome=no_answer / only_if_empty, and
-- the config knobs they cite (cosine_min, min_coverage, t_answer) are English
-- identifiers. A half-translated diagnostic is harder to read than a
-- consistently English one, and harder to paste into a bug report. The chrome
-- around it (headings, column titles, ran/skipped) stays localized.
--
-- Idempotent; transactional.

BEGIN;

DELETE FROM edu_ai.ui_label
 WHERE scope = 'search_test'
   AND label_key IN ('flow_pool', 'flow_from_pool', 'flow_stopped_here');

COMMIT;
