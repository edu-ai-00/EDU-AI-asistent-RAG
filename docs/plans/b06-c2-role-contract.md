# C2: PostgreSQL roles and privileges (contract proposal, verified only on an isolated synthetic lab)

**Scope:** isolated synthetic installations (lab VM). Does not apply to production or school installations; this contract does not cover them yet. Approved by the owner on 2026-10-09 (record `C2_ROLES_APPROVAL.txt`). Replaces the earlier proposal `db-roles-proposal-20261009.md` (phase 1, was not approved for execution).

## 1. Roles

| Role | Login | May |
|---|---|---|
| `edu_ai_admin` | yes, superuser | maintenance, restore, contract management; **never an application credential**, the password stays in `.env`, application containers do not receive it |
| `edu_ai_owner` | no | owns the `edu_ai` schema and application objects; nobody is its member except the migrator (`SET` only, no inheritance) |
| `edu_ai_search` | yes | `edu_ai_search` and QA administration: `SELECT` everywhere, writes only according to the matrix |
| `edu_ai_ingest` | yes | flow components in Langflow (`POSTGRES_DB_USER` / `POSTGRES_PASSWORD`): `SELECT` everywhere, writes only according to the matrix |
| `edu_ai_langflow` | yes | owns only the `langflow` database and its objects; does not connect to the `edu_ai` database |
| `edu_ai_migrator` | yes | only `SET ROLE edu_ai_owner`; after `SET ROLE` it has the owner's privileges |

No role except admin has `SUPERUSER`, `CREATEDB`, `CREATEROLE`, `REPLICATION` or `BYPASSRLS`. Application roles have no ownership, `WITH GRANT OPTION`, `TRUNCATE`, `REFERENCES`, `TRIGGER`, `CREATE` or `TEMPORARY`; on sequences they have only `USAGE` and `SELECT`. `PUBLIC` has no privileges on the databases, the schemas `edu_ai`, `public`, `paradedb`, `pdb`, or on the tables and functions in them. The five login passwords are all different and are generated into the external `.env` (`POSTGRES_ADMIN_PASSWORD`, `POSTGRES_PASSWORD` = the `edu_ai_ingest` password, `POSTGRES_SEARCH_PASSWORD`, `POSTGRES_LANGFLOW_PASSWORD`, `POSTGRES_MIGRATOR_PASSWORD`).

**Name lookup path (`search_path`):** `pg_trgm` (`word_similarity`, the `<->` operator) lives in the `edu_ai` schema and the code calls it without qualification. The earlier single `edu_ai` role found it via "$user". That is why `edu_ai_search` and `edu_ai_ingest` have `search_path = edu_ai, public` set (`ALTER ROLE … SET`); the other roles (admin, owner, migrator, langflow) have no such path, so objects in `edu_ai` cannot intercept them. Without this setting the typo-tolerant fallback step `document_lexical` failed silently (error `function word_similarity(unknown, text) does not exist` in the PostgreSQL log; the step catches the error and continues without candidates). `db_roles.py verify` checks this setting.

**Source of truth:** `postgres/role_contract/matrix.json` (roles, membership, databases, `table_dml`, expected object names). The files `postgres/init_clean/_role_contract_*.psql` are **generated** from it (`python3 scripts/db_roles.py gen-sql`) and are not edited by hand; the test `tests/test_db_role_contract.py` verifies they match.

## 2. What runs where

| Situation | Procedure |
|---|---|
| Clean install | `postgres/init_clean`: `00` (roles, passwords from the container environment, the `langflow` database owned by `edu_ai_langflow`), `01`–`03` schema and seed, `04` (privileges, ownership, the `role-contract:2` marker on the database). The postgres healthcheck turns healthy only after the marker. |
| Existing installation with the single `edu_ai` role | `db_roles.py prepare-env`, `db_roles.py adopt --confirm-isolated`, new compose, `db_roles.py demote-old` (the old role ceases to be `SUPERUSER` → `NOLOGIN`, password `NULL`; the bootstrap superuser cannot lose `SUPERUSER`) |
| After a restore (`pg_restore --no-owner`) and after every migration | `db_roles.py reapply --confirm-isolated`, then `verify` |
| Check | `db_roles.py verify`; `smoke.py` (step 4a) |

Both adoption and `reapply` **refuse to write** if the inventory (names of tables, views, sequences, functions, roles, extensions, membership) does not match the known baseline from `matrix.json`; the check runs before the first write. New tables and functions must be added to the matrix, otherwise the contract rejects them (no automatic default privilege).

## 3. Known limitations and decisions (from the independent review)

- `edu_ai_migrator` is, in terms of impact, equivalent to admin for the owner's objects: through `SET ROLE` it can create triggers and functions that run with admin's privileges during manual maintenance. Do maintenance of the owner's tables as `SET ROLE edu_ai_owner`.
- `edu_ai_ingest` and `edu_ai_search` have `SELECT` on all `edu_ai` tables (broader than the write matrix; including `question_answer_log`, `staff_user`, `app_user`). To be narrowed after measurement.
- The passwords of all roles remain in the environment of the `postgres` container (`docker inspect`). They are not used after initialization.
- The image's `pg_hba.conf` lets `edu_ai_admin` in from the `data` network just like the other roles; denying admin over the network is a recommendation, not part of the contract (pgAdmin would stop working for maintenance).
- The B03e migration tool (`scripts/migrate.py`) runs as the container user (admin), not as the migrator; the objects it creates are owned by admin until `reapply` is performed.
- `template1` has had its `PUBLIC` privileges removed; the roles do not use it anyway.
- `ALTER DEFAULT PRIVILEGES` are set only restrictively (no automatic grant); always run `reapply` after a migration.
