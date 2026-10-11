-- last change in code by Jan Drozd (2026-07-27).
-- 29_qa_query_log_role_school.sql
-- ROLE/ŠKOLA experiment (ukol1.md): log who is asking (žák/učitel/rodič) and
-- which school they're from, alongside the already-logged ročník. Nullable,
-- populated by search/app/log.py. Not yet consumed by the cascade — this
-- column addition only enables the "vyzkoušet zapracování" evaluation
-- (role/school × outcome × answer_source) after a week of production data.
--
-- Incremental migration: init SQL runs only on an empty volume, so
-- apply this by hand on a running DB.
-- Idempotent.

ALTER TABLE edu_ai.qa_query_log ADD COLUMN IF NOT EXISTS user_role text;
ALTER TABLE edu_ai.qa_query_log ADD COLUMN IF NOT EXISTS user_school text;
