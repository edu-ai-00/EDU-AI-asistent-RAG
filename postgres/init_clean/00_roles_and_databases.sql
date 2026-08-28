-- last change in code by Martin Safranek (2026-07-30).
-- 00_roles_and_databases.sql — run against the MAINTENANCE database (postgres).
-- In the docker image the role and the edu_ai database come from POSTGRES_USER /
-- POSTGRES_DB env, so there this script only adds the langflow database. On a
-- managed cluster (RDS…) run it as-is; everything is IF-NOT-EXISTS-guarded.
-- No special grants are needed by design: the single role edu_ai OWNS both
-- databases and every object (verified against the live cluster on 2026-07-29:
-- no ACLs, no default privileges beyond ownership).

SELECT 'CREATE ROLE edu_ai LOGIN PASSWORD ''CHANGE_ME'''
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'edu_ai') \gexec

SELECT 'CREATE DATABASE edu_ai OWNER edu_ai'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'edu_ai') \gexec

-- Langflow's own state database (flows, users, API keys, variables). Langflow
-- bootstraps its schema into it via Alembic on first start.
SELECT 'CREATE DATABASE langflow OWNER edu_ai'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'langflow') \gexec
