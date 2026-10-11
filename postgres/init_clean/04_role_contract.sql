-- 04_role_contract.sql — role contract, part 2: database privileges, ownership and grants
-- in the edu_ai database (generated from postgres/role_contract/matrix.json). Runs after the
-- schema and the seed. The langflow database needs nothing here: it is created with its owner.
-- Refuses before writing if the schema is not the known baseline.
\set ON_ERROR_STOP on
\ir _role_contract_cluster.psql
\ir _role_contract_edu_ai.psql
