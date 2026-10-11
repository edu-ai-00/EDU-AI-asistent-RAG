"""Execute smoke principal SQL on rollback-only fixtures in an isolated reference.

EDU_AI_SMOKE_PRINCIPAL_TEST_CONTAINER=b03e_... and
EDU_AI_SMOKE_PRINCIPAL_TEST_CONTEXT=default opt in to the real PostgreSQL cases.
The container must carry the clean-organizations-reference purpose label,
network none and no published ports. No application or Compose stack is called.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import smoke


class RealPrincipalSqlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.container = os.environ.get("EDU_AI_SMOKE_PRINCIPAL_TEST_CONTAINER", "")
        cls.context = os.environ.get("EDU_AI_SMOKE_PRINCIPAL_TEST_CONTEXT", "")
        if not cls.container or not cls.context:
            raise unittest.SkipTest("explicit isolated PostgreSQL reference not selected")
        if not cls.container.startswith("b03e_"):
            raise RuntimeError("refusing a container outside the isolated reference namespace")
        cls.docker = ["docker", "--context", cls.context]
        endpoint = subprocess.check_output(cls.docker + ["context", "inspect", cls.context,
                                             "--format", "{{.Endpoints.docker.Host}}"], text=True).strip()
        if not endpoint.startswith(("npipe://", "unix://")):
            raise RuntimeError("reference tests require a local Docker endpoint")
        info = json.loads(subprocess.check_output(cls.docker + ["inspect", cls.container]))[0]
        if (info["Config"].get("Labels", {}).get("edu_ai.purpose") != "clean-organizations-reference" or
            info["HostConfig"]["NetworkMode"] != "none" or info["HostConfig"]["PortBindings"] or
            len(info["Mounts"]) != 1 or info["Mounts"][0].get("Name") != cls.container + "_data"):
            raise RuntimeError("refusing a reference without the required isolation")
        cls.psql = cls.docker + ["exec", "-i", cls.container, "psql", "-X", "-q", "-A", "-t",
                                "-v", "ON_ERROR_STOP=1", "-U", "edu_ai_admin", "-d", "edu_ai"]
        clean = subprocess.run(cls.psql, input="SELECT count(*) FROM edu_ai.staff_user s "
                               "JOIN edu_ai.legal_entity e USING (legal_entity_code) "
                               "WHERE s.is_active AND e.is_active;", capture_output=True, text=True)
        if clean.returncode or clean.stdout.strip() != "0":
            raise RuntimeError("reference must be the clean seed without a registered active principal")

    def setUp(self):
        self.users = []
        smoke._passed = smoke._failed = smoke._skipped = 0
        self.before = self.query("SELECT count(*) FROM edu_ai.staff_user;").stdout.strip()

    def query(self, sql):
        p = subprocess.run(self.psql, input=sql, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(p.returncode, 0, p.stderr)
        return p

    def execute_principal_sql(self, argv):
        # Replace only the transport. The helper's exact SELECT executes against
        # real schema/FKs; all fixture INSERTs roll back in that same session.
        self.assertEqual(argv[:5], ["docker", "compose", "exec", "-T", "postgres"])
        self.assertEqual(argv[-2], "-tAc")
        fixtures = "INSERT INTO edu_ai.legal_entity (legal_entity_code,display_name,is_active) VALUES " \
                   "('SMOKE_TEST_ACTIVE','isolated fixture',true)," \
                   "('SMOKE_TEST_INACTIVE','isolated fixture',false);\n"
        for entity, user, active in self.users:
            fixtures += "INSERT INTO edu_ai.staff_user (legal_entity_code,user_id,display_name,is_active) " \
                        f"VALUES ('{entity}','{user}','isolated fixture',{'true' if active else 'false'});\n"
        return self.query("BEGIN;\n" + fixtures + argv[-1] + "\nROLLBACK;\n")

    def principal(self):
        with patch.object(smoke, "run", side_effect=self.execute_principal_sql):
            return smoke._a_real_principal({"POSTGRES_DB": "edu_ai"})

    def tearDown(self):
        self.assertEqual(self.query("SELECT count(*) FROM edu_ai.staff_user;").stdout.strip(), self.before)
        self.assertEqual(self.query("SELECT count(*) FROM edu_ai.legal_entity "
                                   "WHERE legal_entity_code LIKE 'SMOKE_TEST_%';").stdout.strip(), "0")

    def test_active_user_in_inactive_entity_is_rejected(self):
        self.users = [("SMOKE_TEST_INACTIVE", "smoke-active-on-inactive", True)]
        self.assertIsNone(self.principal())

    def test_inactive_user_in_active_entity_is_rejected(self):
        self.users = [("SMOKE_TEST_ACTIVE", "smoke-inactive-on-active", False)]
        self.assertIsNone(self.principal())

    def test_valid_pair_is_selected_among_invalid_pairs(self):
        self.users = [("SMOKE_TEST_INACTIVE", "smoke-active-on-inactive", True),
                      ("SMOKE_TEST_ACTIVE", "smoke-inactive-on-active", False),
                      ("SMOKE_TEST_ACTIVE", "smoke-valid", True)]
        self.assertEqual(self.principal(), ("SMOKE_TEST_ACTIVE", "smoke-valid"))

    def test_no_valid_pair_returns_none(self):
        self.users = [("SMOKE_TEST_INACTIVE", "smoke-active-on-inactive", True),
                      ("SMOKE_TEST_ACTIVE", "smoke-inactive-on-active", False)]
        self.assertIsNone(self.principal())

    def test_no_valid_pair_marks_dry_run_skip_without_a_pass(self):
        with patch.object(smoke, "run", side_effect=self.execute_principal_sql), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            smoke.check_dry_run_ingest({"POSTGRES_DB": "edu_ai"})
        self.assertEqual((smoke._passed, smoke._failed, smoke._skipped), (0, 0, 1))
        self.assertIn("no active staff_user in an active entity", output.getvalue())


if __name__ == "__main__":
    unittest.main()
