-- last change in code by Martin Safranek (2026-08-01).
-- 54_qa_query_log_flow_trace.sql
-- qa_query_log.flow_trace — the per-step cascade trace, persisted with the turn.
--
-- The existing columns say WHERE the answer came from (answer_source,
-- strategies) but not how the cascade got there: which steps ran, which were
-- skipped and why, what each returned, how long each took, and which one ended
-- the walk. `strategies` records only the steps that CONTRIBUTED, so a skipped
-- step or a step that ran and found nothing leaves no trace at all — exactly the
-- information needed when an answer is wrong.
--
-- Shape (list, pipeline order — see executor.run_search):
--   [{"step": "qa_lexical", "status": "ran", "ms": 4, "decision": "B-deferred",
--     "match_score": 0.5137},
--    {"step": "rag_keywords", "status": "skipped", "pool": 30,
--     "max_coverage": 0.667, "note": "only_if_empty and the pool is not empty"},
--    {"step": "answer", "status": "ran", "ms": 1185, "resolved": true,
--     "outcome": "answer", "stopped_here": true}]
--
-- status is one of: ran | skipped | disabled | unknown | error.
--
-- Nullable and written fire-and-forget like the rest of the row: a turn logged
-- by an older build simply has NULL here.
--
-- Idempotent; transactional.

BEGIN;

ALTER TABLE edu_ai.qa_query_log
    ADD COLUMN IF NOT EXISTS flow_trace jsonb;

COMMENT ON COLUMN edu_ai.qa_query_log.flow_trace IS
    'Per-step cascade trace in pipeline order: which steps ran/were skipped, '
    'why, their timing and what ended the walk. Diagnostic; see migration 54.';

COMMIT;
