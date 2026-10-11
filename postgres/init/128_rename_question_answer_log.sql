-- 128: rename qa_query_log -> question_answer_log (2026-08-09).
-- The table long ago outgrew "QA": it records EVERY search turn (RAG included),
-- its flow_trace now carries per-phase chunk ids and the LLM-context provenance.
-- Pure rename — no column changes; dependent objects (qa_conversation_v,
-- qa_feedback FK) follow automatically via OIDs, only their NAMES are synced.
-- init_clean/01_schema.sql carries the same names for fresh installs.

ALTER TABLE edu_ai.qa_query_log RENAME TO question_answer_log;

ALTER SEQUENCE edu_ai.qa_query_log_new_id_seq RENAME TO question_answer_log_id_seq;

ALTER TABLE edu_ai.question_answer_log
    RENAME CONSTRAINT qa_query_log_new_pkey TO question_answer_log_pkey;
ALTER TABLE edu_ai.question_answer_log
    RENAME CONSTRAINT qa_query_log_setting_id_fkey TO question_answer_log_setting_id_fkey;

ALTER INDEX edu_ai.qa_query_log_conv_idx RENAME TO question_answer_log_conv_idx;
ALTER INDEX edu_ai.qa_query_log_created_idx RENAME TO question_answer_log_created_idx;
ALTER INDEX edu_ai.qa_query_log_setting_idx RENAME TO question_answer_log_setting_idx;
ALTER INDEX edu_ai.qa_query_log_source_idx RENAME TO question_answer_log_source_idx;
ALTER INDEX edu_ai.qa_query_log_legal_entity_idx RENAME TO question_answer_log_legal_entity_idx;
