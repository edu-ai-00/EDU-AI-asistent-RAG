-- 00_roles_and_databases.sql — role contract, part 1: roles, passwords, the langflow database.
-- In the docker image the bootstrap role (edu_ai_admin, the maintenance superuser) and the
-- edu_ai database come from POSTGRES_USER / POSTGRES_DB; this script adds the other roles
-- (generated from postgres/role_contract/matrix.json) and the langflow database owned by
-- its own role. Passwords come from the container environment, never from this text.
-- On a managed cluster run it as a maintenance role with the same variables set.
\set ON_ERROR_STOP on

-- the contract is written for the database named edu_ai
SELECT current_database() = 'edu_ai' AS db_ok \gset
\if :db_ok
\else
DO $$ BEGIN RAISE EXCEPTION 'the role contract needs POSTGRES_DB=edu_ai'; END $$;
\endif

\ir _role_contract_roles.psql

-- passwords: five distinct generated values (the admin one is POSTGRES_PASSWORD of this container),
-- none of them a template placeholder; clear text stays out of the server log
SET log_statement = 'none';
SET log_min_error_statement = 'panic';
\set pw_admin `printenv POSTGRES_PASSWORD`
\set pw_ingest `printenv POSTGRES_INGEST_PASSWORD`
\set pw_search `printenv POSTGRES_SEARCH_PASSWORD`
\set pw_langflow `printenv POSTGRES_LANGFLOW_PASSWORD`
\set pw_migrator `printenv POSTGRES_MIGRATOR_PASSWORD`
SELECT (SELECT bool_and(length(p) >= 16 AND p !~ '^CHANGE_ME') AND count(DISTINCT p) = 5
        FROM (VALUES (:'pw_admin'), (:'pw_ingest'), (:'pw_search'), (:'pw_langflow'), (:'pw_migrator')) v(p)) AS pw_ok \gset
\if :pw_ok
\else
DO $$ BEGIN RAISE EXCEPTION 'role passwords are missing from the environment, are placeholders, or are not distinct (each at least 16 characters)'; END $$;
\endif
ALTER ROLE edu_ai_ingest PASSWORD :'pw_ingest';
ALTER ROLE edu_ai_search PASSWORD :'pw_search';
ALTER ROLE edu_ai_langflow PASSWORD :'pw_langflow';
ALTER ROLE edu_ai_migrator PASSWORD :'pw_migrator';
\unset pw_admin
\unset pw_ingest
\unset pw_search
\unset pw_langflow
\unset pw_migrator
RESET log_statement;
RESET log_min_error_statement;

SELECT 'CREATE DATABASE edu_ai OWNER edu_ai_admin'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'edu_ai') \gexec

-- Langflow's own state database (flows, users, API keys, variables). Langflow
-- bootstraps its schema into it via Alembic on first start, as this role.
SELECT 'CREATE DATABASE langflow OWNER edu_ai_langflow'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'langflow') \gexec
