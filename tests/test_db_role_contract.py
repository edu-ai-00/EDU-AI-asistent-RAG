#!/usr/bin/env python3
"""DB role contract: generator, wiring and (optionally) a live synthetic cluster.

    python3 tests/test_db_role_contract.py                       # offline part only
    EDU_AI_ROLE_TEST_CONTAINER=<name> EDU_AI_ROLE_TEST_ENV=<env file> python3 tests/test_db_role_contract.py

The live part needs an isolated synthetic PostgreSQL started from postgres/init_clean (see
docs/plans/b06-c2-role-contract.md), never a production or school database. It prints a
structured matrix (role, probe, outcome) and never a password or row content.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import db_roles  # noqa: E402
import install  # noqa: E402
import bootstrap  # noqa: E402
from envfile import read_env  # noqa: E402

M = db_roles.load_matrix()
SECRETS = ("POSTGRES_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "POSTGRES_SEARCH_PASSWORD",
           "POSTGRES_LANGFLOW_PASSWORD", "POSTGRES_MIGRATOR_PASSWORD")


def service_block(compose: str, name: str) -> str:
    m = re.search(rf"^  {re.escape(name)}:\r?\n(.*?)(?=^  [a-z_0-9]+:\r?\n|^[a-z]+:\r?\n|\Z)", compose, re.S | re.M)
    assert m, name
    return m.group(1)


class GeneratorTests(unittest.TestCase):
    def test_committed_files_are_generated_output(self):
        for name, text in db_roles.generate(M).items():
            on_disk = (ROOT / "postgres" / "init_clean" / name).read_bytes().decode("utf-8").replace("\r\n", "\n")
            self.assertEqual(on_disk, text, f"{name} was edited by hand or the matrix changed: run db_roles.py gen-sql")

    def test_extension_privileges_are_the_contracts_own(self):
        # bootstrap re-runs these after ALTER EXTENSION UPDATE; they must be the clean install's statements
        edu_ai = db_roles.gen_edu_ai(M).splitlines()
        for line in db_roles.extension_privileges(M):
            self.assertIn(line, edu_ai)

    def test_matrix_forbids_what_the_contract_forbids(self):
        for role in ("edu_ai_search", "edu_ai_ingest", "edu_ai_langflow", "edu_ai_migrator", "edu_ai_owner"):
            a = M["roles"][role]
            self.assertFalse(any(a[k] for k in ("superuser", "createdb", "createrole", "replication", "bypassrls")), role)
        self.assertFalse(M["roles"]["edu_ai_owner"]["login"])
        self.assertEqual(M["memberships"], [{"role": "edu_ai_owner", "member": "edu_ai_migrator", "inherit": False, "set": True}])
        for table, per in M["edu_ai_db"]["table_dml"].items():
            for role, privs in per.items():
                self.assertIn(role, ("edu_ai_search", "edu_ai_ingest"), table)
        self.assertEqual(M["databases"]["edu_ai"]["connect"], ["edu_ai_search", "edu_ai_ingest", "edu_ai_migrator"])
        self.assertEqual(M["databases"]["langflow"]["connect"], ["edu_ai_langflow"])
        self.assertEqual(M["login_secrets"]["edu_ai_admin"], "POSTGRES_ADMIN_PASSWORD")
        self.assertEqual(len(set(M["login_secrets"].values())), 5)

    def test_identifiers_are_validated(self):
        bad = json.loads(json.dumps(M))
        bad["edu_ai_db"]["table_dml"]["x; drop table y"] = {}
        p = pathlib.Path(os.environ.get("TMPDIR", "/tmp")) / "bad_matrix.json"
        p.write_text(json.dumps(bad))
        with self.assertRaises(db_roles.ContractError):
            db_roles.load_matrix(p)
        p.unlink()

    def test_guard_runs_before_any_write(self):
        for name in ("_role_contract_edu_ai.psql", "_role_contract_langflow.psql"):
            text = db_roles.generate(M)[name]
            self.assertLess(text.index("baseline mismatch"), text.index("BEGIN;"), name)
            self.assertNotRegex(text[:text.index("BEGIN;")], r"(?im)^\s*(ALTER|GRANT|REVOKE|CREATE|DROP)\b", name)

    def test_no_sequence_update_and_no_truncate_grant(self):
        sql = db_roles.generate(M)["_role_contract_edu_ai.psql"]
        self.assertNotRegex(sql, r"(?i)GRANT[^;]*\bTRUNCATE\b")
        self.assertNotRegex(sql, r"(?i)GRANT[^;]*SEQUENCE[^;]*\bUPDATE\b")
        self.assertNotRegex(sql, r"(?i)WITH GRANT OPTION")


class SafetyTests(unittest.TestCase):
    def test_every_name_that_reaches_sql_is_validated(self):
        for path, value in (("baseline.edu_ai.functions", "x'); drop table y; --"), ("legacy_roles", "a b"), ("public_revoked_databases", "t;1")):
            bad = json.loads(json.dumps(M))
            node = bad
            *head, last = path.split(".")
            for k in head:
                node = node[k]
            node[last] = [value]
            p = pathlib.Path(os.environ.get("TMPDIR", "/tmp")) / "bad_matrix2.json"
            p.write_text(json.dumps(bad))
            with self.assertRaises(db_roles.ContractError, msg=path):
                db_roles.load_matrix(p)
            p.unlink()

    def test_secrets_are_checked_before_anything_is_written(self):
        good = {k: "A" * 15 + str(i) + "x" for i, k in enumerate(M["login_secrets"].values())}
        self.assertEqual(len(db_roles.secrets_from(good, M)), 5)
        for mutate in (lambda e: e.update(POSTGRES_SEARCH_PASSWORD="CHANGE_ME_postgres_search_password"),
                       lambda e: e.update(POSTGRES_SEARCH_PASSWORD=e["POSTGRES_PASSWORD"]),
                       lambda e: e.update(POSTGRES_ADMIN_PASSWORD="short"),
                       lambda e: e.update(POSTGRES_LANGFLOW_PASSWORD="has space in it 1234"),
                       lambda e: e.pop("POSTGRES_MIGRATOR_PASSWORD"),
                       lambda e: e.update(POSTGRES_PASSWORD="ok" * 8 + "\n")):
            env = dict(good)
            mutate(env)
            with self.assertRaises(db_roles.ContractError):
                db_roles.secrets_from(env, M)

    def test_writing_commands_need_the_isolation_flag(self):
        import argparse
        for fn in (db_roles.cmd_reapply, db_roles.cmd_demote):
            self.assertEqual(fn(argparse.Namespace(container="none", admin_user="x", old_role="edu_ai", confirm_isolated=False)), 1)

    def test_prepare_env_adds_only_the_missing_secrets_and_keeps_everything_else(self):
        import argparse
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp, ".env")
            keep = "A" * 12 + "keep-this-one-1234"
            path.write_text("# c\nPOSTGRES_DB_USER=edu_ai\nPOSTGRES_PASSWORD=" + keep + "\nPOSTGRES_SEARCH_PASSWORD=CHANGE_ME_x\nOTHER=1\n", encoding="utf-8")
            import io
            import contextlib
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(db_roles.cmd_prepare_env(argparse.Namespace(env=str(path))), 0)
            env = read_env(path)
            self.assertEqual((env["POSTGRES_PASSWORD"], env["OTHER"], env["POSTGRES_DB_USER"]), (keep, "1", "edu_ai_ingest"))
            for key in ("POSTGRES_ADMIN_PASSWORD", "POSTGRES_SEARCH_PASSWORD", "POSTGRES_LANGFLOW_PASSWORD", "POSTGRES_MIGRATOR_PASSWORD"):
                self.assertRegex(env[key], r"^[A-Za-z0-9_-]{32}$")
                self.assertNotIn(env[key], out.getvalue())
            self.assertEqual(len({env[k] for k in M["login_secrets"].values()}), 5)
            first = path.read_bytes()
            with contextlib.redirect_stdout(io.StringIO()):
                db_roles.cmd_prepare_env(argparse.Namespace(env=str(path)))
            self.assertEqual(path.read_bytes(), first)                      # a second run changes nothing
            self.assertEqual(db_roles.cmd_prepare_env(argparse.Namespace(env=str(pathlib.Path(tmp, "missing")))), 1)

    def test_demote_refuses_a_role_that_is_not_legacy(self):
        import argparse
        self.assertEqual(db_roles.cmd_demote(argparse.Namespace(container="none", admin_user="x", old_role="edu_ai_search", confirm_isolated=True)), 1)

    def test_generated_sql_pins_the_search_path_and_a_lock_timeout(self):
        for name, text in db_roles.generate(M).items():
            self.assertIn("SET search_path = pg_catalog, pg_temp;", text, name)
        for name in ("_role_contract_edu_ai.psql", "_role_contract_langflow.psql"):
            self.assertIn("SET LOCAL lock_timeout", db_roles.generate(M)[name])

    def test_only_the_application_roles_get_a_search_path(self):
        sql = db_roles.generate(M)["_role_contract_roles.psql"]
        for role in ("edu_ai_search", "edu_ai_ingest"):
            self.assertIn(f"ALTER ROLE {role} SET search_path = edu_ai, public;", sql)
        for role in ("edu_ai_admin", "edu_ai_owner", "edu_ai_migrator", "edu_ai_langflow"):
            self.assertIn(f"ALTER ROLE {role} RESET search_path;", sql)
            self.assertNotIn(f"ALTER ROLE {role} SET search_path", sql)

    def test_no_default_privilege_grants_and_no_public_defaults(self):
        sql = db_roles.generate(M)["_role_contract_edu_ai.psql"]
        self.assertNotRegex(sql, r"ALTER DEFAULT PRIVILEGES[^;]*\bGRANT\b")
        self.assertNotRegex(sql, r"ALTER DEFAULT PRIVILEGES[^;]*GRANT")
        self.assertIn("REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC", sql)

    def test_the_init_demands_distinct_non_placeholder_passwords(self):
        text = (ROOT / "postgres/init_clean/00_roles_and_databases.sql").read_text(encoding="utf-8")
        self.assertIn("CHANGE_ME", text)
        self.assertIn("count(DISTINCT p) = 5", text)
        self.assertIn("SET log_statement = 'none'", text)


class WiringTests(unittest.TestCase):
    compose = (ROOT / "docker-compose.yml").read_bytes().decode("utf-8")
    template = (ROOT / ".env.example").read_bytes().decode("utf-8")

    def test_every_secret_is_generated_and_templated(self):
        for key in SECRETS:
            self.assertIn(key, install.GENERATED)
            self.assertIn(key, install.GENERATORS)
            self.assertIn(key, bootstrap.GENERATED_SECRETS)
            self.assertRegex(self.template, rf"(?m)^{key}=CHANGE_ME")
        self.assertEqual(set(install.GENERATED), set(bootstrap.GENERATED_SECRETS))
        self.assertRegex(self.template, r"(?m)^POSTGRES_DB_USER=edu_ai_ingest\r?$")

    def test_login_secret_names_match_the_matrix(self):
        self.assertEqual(set(M["login_secrets"].values()), set(SECRETS))

    def test_postgres_service_gets_the_role_secrets(self):
        block = service_block(self.compose, "postgres")
        self.assertIn("POSTGRES_USER: edu_ai_admin", block)
        self.assertIn("POSTGRES_PASSWORD: ${POSTGRES_ADMIN_PASSWORD}", block)
        for env_name, key in (("POSTGRES_INGEST_PASSWORD", "POSTGRES_PASSWORD"), ("POSTGRES_SEARCH_PASSWORD", "POSTGRES_SEARCH_PASSWORD"),
                              ("POSTGRES_LANGFLOW_PASSWORD", "POSTGRES_LANGFLOW_PASSWORD"), ("POSTGRES_MIGRATOR_PASSWORD", "POSTGRES_MIGRATOR_PASSWORD")):
            self.assertIn(f"{env_name}: ${{{key}}}", block)
        self.assertIn("like 'role-contract:%'", block)         # healthy only once the contract is applied
        self.assertNotIn("pg_isready", block)

    def test_applications_never_receive_the_admin_secret(self):
        for name in re.findall(r"(?m)^  ([a-z_0-9]+):\r?$", self.compose):
            if name == "postgres" or name not in self.compose:
                continue
            block = service_block(self.compose, name)
            for m in re.finditer(r"POSTGRES_ADMIN_PASSWORD[^\r\n]*", block):
                self.assertRegex(m.group(0), r"POSTGRES_ADMIN_PASSWORD=\s*$", f"{name}: {m.group(0)}")

    def test_langflow_keeps_env_file_but_blanks_foreign_secrets(self):
        block = service_block(self.compose, "edu_ai")
        self.assertIn("env_file:", block)
        for key in ("POSTGRES_ADMIN_PASSWORD", "POSTGRES_SEARCH_PASSWORD", "POSTGRES_MIGRATOR_PASSWORD", "POSTGRES_LANGFLOW_PASSWORD",
                    "BIFROST_SETUP_TOKEN", "BIFROST_ADMIN_PASSWORD"):
            self.assertRegex(block, rf"(?m)^\s+- {key}=\r?$")
        self.assertIn("LANGFLOW_DATABASE_URL=postgresql://edu_ai_langflow:${POSTGRES_LANGFLOW_PASSWORD}@", block)

    def test_search_uses_its_own_role(self):
        block = service_block(self.compose, "edu_ai_search")
        self.assertIn("PG_USER=edu_ai_search", block)
        self.assertIn("PG_PASSWORD=${POSTGRES_SEARCH_PASSWORD}", block)
        self.assertNotIn("POSTGRES_PASSWORD}", block.replace("POSTGRES_SEARCH_PASSWORD}", ""))

    def test_init_files_use_environment_for_passwords(self):
        text = (ROOT / "postgres/init_clean/00_roles_and_databases.sql").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"PASSWORD '([^']*)'", text), [])
        for var in ("POSTGRES_INGEST_PASSWORD", "POSTGRES_SEARCH_PASSWORD", "POSTGRES_LANGFLOW_PASSWORD", "POSTGRES_MIGRATOR_PASSWORD"):
            self.assertIn(var, text)


ENV_FILE = os.environ.get("EDU_AI_ROLE_TEST_ENV")
CONTAINER = os.environ.get("EDU_AI_ROLE_TEST_CONTAINER")


@unittest.skipUnless(CONTAINER and ENV_FILE, "set EDU_AI_ROLE_TEST_CONTAINER and EDU_AI_ROLE_TEST_ENV for the live part")
class LiveClusterTests(unittest.TestCase):
    results: list[dict] = []

    @classmethod
    def setUpClass(cls):
        cls.env = read_env(ENV_FILE)
        lines = "".join(f"127.0.0.1:5432:*:{r}:{cls.env[k]}\n" for r, k in M["login_secrets"].items())
        subprocess.run(["docker", "exec", "-i", CONTAINER, "sh", "-c", "umask 077; cat > /tmp/c2pgpass"], input=lines, text=True, check=True)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["docker", "exec", CONTAINER, "rm", "-f", "/tmp/c2pgpass"], check=False)
        out = os.environ.get("EDU_AI_ROLE_TEST_OUT")
        if out:
            pathlib.Path(out).write_text(json.dumps(cls.results, indent=1), encoding="utf-8")

    def psql(self, role: str, db: str, sql: str):
        return subprocess.run(["docker", "exec", "-i", "-e", "PGPASSFILE=/tmp/c2pgpass", "-e", "PGCONNECT_TIMEOUT=10",  CONTAINER, "psql", "-h", "127.0.0.1", "-U", role, "-d", db,
                               "-X", "-At", "-v", "ON_ERROR_STOP=1", "-v", "VERBOSITY=sqlstate"], input=sql, capture_output=True, text=True)

    def denied(self, role: str, db: str, name: str, sql: str):
        r = self.psql(role, db, sql)
        self.results.append({"role": role, "db": db, "probe": name, "expected": "denied", "outcome": "denied" if r.returncode else "ALLOWED"})
        self.assertNotEqual(r.returncode, 0, f"{role}: {name} was allowed")
        # server messages are localised: judge by SQLSTATE (42501 insufficient privilege) or a failed connection (FATAL)
        self.assertTrue("42501" in r.stderr or "fatal" in r.stderr.lower(), f"{role}: {name}: unexpected failure kind: {r.stderr.strip()[-120:]}")

    def ineffective(self, role: str, db: str, name: str, sql: str):
        """GRANT by a non-owner is a warning, not an error: it must either fail or change nothing."""
        r = self.psql(role, db, sql)
        self.results.append({"role": role, "db": db, "probe": name, "expected": "no effect",
                             "outcome": "no effect" if (r.returncode or "01007" in r.stderr) else "EFFECT?"})
        self.assertTrue(r.returncode != 0 or "01007" in r.stderr, f"{role}: {name}: {r.stderr.strip()[-160:]}")

    def allowed(self, role: str, db: str, name: str, sql: str):
        r = self.psql(role, db, sql)
        self.results.append({"role": role, "db": db, "probe": name, "expected": "allowed", "outcome": "allowed" if r.returncode == 0 else "DENIED"})
        self.assertEqual(r.returncode, 0, f"{role}: {name} failed: {r.stderr.strip()[-160:]}")

    def test_catalog_matches_matrix(self):
        self.assertEqual(db_roles.verify(CONTAINER, "edu_ai_admin", M), [])

    def test_logins_and_cross_database_connect(self):
        for role in ("edu_ai_search", "edu_ai_ingest", "edu_ai_migrator"):
            self.allowed(role, "edu_ai", "connect edu_ai", "select 1;")
            self.denied(role, "langflow", "connect langflow", "select 1;")
            self.denied(role, "postgres", "connect postgres", "select 1;")
        self.allowed("edu_ai_langflow", "langflow", "connect langflow", "select 1;")
        self.denied("edu_ai_langflow", "edu_ai", "connect edu_ai", "select 1;")
        self.denied("edu_ai_langflow", "postgres", "connect postgres", "select 1;")

    def test_runtime_roles_cannot_do_cluster_or_ddl_things(self):
        probes = {
            "create table in edu_ai": "create table edu_ai.c2_probe(x int);",
            "create table in public": "create table public.c2_probe(x int);",
            "create schema": "create schema c2_probe;",
            "drop table": "drop table edu_ai.settings;",
            "truncate": "truncate edu_ai.settings;",
            "alter table": "alter table edu_ai.settings add column c2_probe int;",
            "create index": "create index c2_probe on edu_ai.settings(setting_id);",
            "create role": "create role c2_probe;",
            "alter role": "alter role edu_ai_search superuser;",
            "set role owner": "set role edu_ai_owner;",
            "set role admin": "set role edu_ai_admin;",
            "create extension": "create extension if not exists hstore;",
            "alter system": "alter system set work_mem = '64MB';",
            "copy program": "copy (select 1) to program 'true';",
            "read file": "select pg_read_file('/etc/passwd');",
            "create database": "create database c2_probe;",
            "disable trigger": "alter table edu_ai.settings disable trigger all;",
            "create function": "create function edu_ai.c2_probe() returns int language sql as 'select 1';",
            "create temp table": "create temp table c2_probe(x int);",
        }
        for role in ("edu_ai_search", "edu_ai_ingest"):
            for name, sql in probes.items():
                self.denied(role, "edu_ai", name, sql)

    def test_grant_by_a_runtime_role_has_no_effect(self):
        for role in ("edu_ai_search", "edu_ai_ingest"):
            self.ineffective(role, "edu_ai", "grant select to public", "grant select on edu_ai.settings to public;")
            self.ineffective(role, "edu_ai", "grant dml to self", "grant delete on edu_ai.settings to " + role + ";")

    def test_zz_catalog_unchanged_by_the_probes(self):
        self.assertEqual(db_roles.verify(CONTAINER, "edu_ai_admin", M), [])

    def test_application_roles_resolve_the_trigram_functions_unqualified(self):
        for role in ("edu_ai_search", "edu_ai_ingest"):
            self.allowed(role, "edu_ai", "show search_path", "show search_path;")
            self.allowed(role, "edu_ai", "unqualified word_similarity", "select word_similarity('otevira', 'otevírací doba');")
            self.allowed(role, "edu_ai", "unqualified <-> operator", "select 'abc' <-> 'abd';")
        r = self.psql("edu_ai_migrator", "edu_ai", "select word_similarity('a', 'b');")            # no schema in its path: 42883, not a privilege error
        self.assertTrue(r.returncode != 0 and "42883" in r.stderr, r.stderr.strip()[-120:])

    def test_dml_matches_the_matrix_for_a_sample(self):
        self.allowed("edu_ai_search", "edu_ai", "select settings", "select count(*) from edu_ai.settings;")
        self.allowed("edu_ai_ingest", "edu_ai", "select settings", "select count(*) from edu_ai.settings;")
        self.denied("edu_ai_search", "edu_ai", "delete settings", "delete from edu_ai.settings where false;")
        self.denied("edu_ai_ingest", "edu_ai", "update settings", "update edu_ai.settings set is_active = is_active where false;")
        self.denied("edu_ai_search", "edu_ai", "delete document_chunks", "delete from edu_ai.document_chunks where false;")
        self.allowed("edu_ai_ingest", "edu_ai", "delete document_chunks", "begin; delete from edu_ai.document_chunks where false; rollback;")
        self.allowed("edu_ai_search", "edu_ai", "delete qa_entries", "begin; delete from edu_ai.qa_entries where false; rollback;")
        self.denied("edu_ai_ingest", "edu_ai", "delete qa_entries", "delete from edu_ai.qa_entries where false;")

    def test_migrator_only_through_set_role(self):
        self.denied("edu_ai_migrator", "edu_ai", "ddl without set role", "create table edu_ai.c2_probe(x int);")
        self.denied("edu_ai_migrator", "edu_ai", "select without set role", "select count(*) from edu_ai.settings;")
        self.allowed("edu_ai_migrator", "edu_ai", "ddl as owner (rolled back)", "begin; set role edu_ai_owner; create table edu_ai.c2_probe(x int); rollback;")
        self.denied("edu_ai_migrator", "edu_ai", "set role admin", "set role edu_ai_admin;")
        self.denied("edu_ai_migrator", "edu_ai", "grant owner to self", "grant edu_ai_owner to edu_ai_migrator;")
        self.allowed("edu_ai_migrator", "edu_ai", "owner cannot create roles", "begin; set role edu_ai_owner; select 1; rollback;")
        self.denied("edu_ai_migrator", "edu_ai", "create role as owner", "set role edu_ai_owner; create role c2_probe;")

    def test_langflow_role_owns_only_its_database(self):
        self.allowed("edu_ai_langflow", "langflow", "ddl in own database (rolled back)", "begin; create table public.c2_probe(x int); rollback;")
        self.denied("edu_ai_langflow", "langflow", "create role", "create role c2_probe;")
        self.denied("edu_ai_langflow", "langflow", "set role owner", "set role edu_ai_owner;")
        self.denied("edu_ai_langflow", "langflow", "alter system", "alter system set work_mem = '64MB';")
        self.denied("edu_ai_langflow", "langflow", "read file", "select pg_read_file('/etc/passwd');")


if __name__ == "__main__":
    unittest.main(verbosity=2)
