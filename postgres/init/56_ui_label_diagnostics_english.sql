-- last change in code by Martin Safranek (2026-08-01).
-- 56_ui_label_diagnostics_english.sql
-- The search page's DIAGNOSTIC panels — the cascade trace and the qa_query_log
-- row — are English-only in every interface language, headings and column
-- titles included, so their labels leave the číselník.
--
-- Why: the vocabulary is English on both ends. The steps emit decision=A /
-- outcome=no_answer / only_if_empty, the knobs they cite are English identifiers
-- (cosine_min, min_coverage, t_answer) and the log's column names are the
-- database's own. A Czech heading over English content is the worst of both, and
-- a trace pasted into a bug report should read the same for everyone.
--
-- The page's own chrome (title, search button, filter hint, language names)
-- stays localized — that is what a user reads, not what a developer debugs.
--
-- Idempotent; transactional.

BEGIN;

DELETE FROM edu_ai.ui_label
 WHERE scope = 'search_test'
   AND label_key IN ('flow_heading', 'flow_step', 'flow_status', 'flow_detail',
                     'flow_ms', 'flow_ran', 'flow_skipped', 'flow_disabled',
                     'flow_error', 'log_heading', 'log_waiting', 'log_note');

COMMIT;
