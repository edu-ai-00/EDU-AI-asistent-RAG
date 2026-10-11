-- 129: index audit follow-up (2026-08-09).
-- 1) qa_feedback.log_id — the FK to question_answer_log(id) had no index, so
--    every feedback<->log join (and any future log cleanup) was a seq scan.
-- 2) document_chunks.file_item_uuid / file_uuid — document_siblings retrieves
--    by `= ANY(...)` on both; the step is currently disabled in the pipeline,
--    but enabling it on server-sized data would full-scan the table per query.
-- 3) prompt_registry_active_lookup was a plain copy of the UNIQUE
--    prompt_registry_one_active (both btree(prompt_key) WHERE is_active).
-- init_clean/01_schema.sql carries the same state for fresh installs.

CREATE INDEX IF NOT EXISTS qa_feedback_log_idx
    ON edu_ai.qa_feedback (log_id);

CREATE INDEX IF NOT EXISTS chunks_file_item_uuid_idx
    ON edu_ai.document_chunks (file_item_uuid);

CREATE INDEX IF NOT EXISTS chunks_file_uuid_idx
    ON edu_ai.document_chunks (file_uuid);

DROP INDEX IF EXISTS edu_ai.prompt_registry_active_lookup;
