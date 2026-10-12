"""Registry and runner against isolated PostgreSQL (B03e-1). Synthetic databases only.

Prerequisite (own containers, nothing from the dev stack):
    python tests/migrate_test_env.py up b03e_pg_a
    python tests/migrate_test_env.py up b03e_pg_b          # optional: independence test
Run:
    EDU_AI_MIGRATE_TEST_CONTAINER=b03e_pg_a EDU_AI_MIGRATE_TEST_CONTAINER_2=b03e_pg_b \
        python tests/test_migrate_runner.py
Without the container variable every test is skipped (and says so).
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from eduai_migrate import commands, fingerprint, manifest, pg  # noqa: E402

C1 = os.environ.get("EDU_AI_MIGRATE_TEST_CONTAINER")
C2 = os.environ.get("EDU_AI_MIGRATE_TEST_CONTAINER_2")
USER, TEMPLATE = "edu_ai_admin", "edu_ai_tpl"
needs_db = unittest.skipUnless(C1, "EDU_AI_MIGRATE_TEST_CONTAINER not set (see module docstring)")

M217 = "ALTER TABLE edu_ai.settings ADD COLUMN b03e_note text;\n"
M218 = "ALTER TABLE edu_ai.settings ADD COLUMN b03e_note2 int;\n"


class Base(unittest.TestCase):
    container = C1

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="b03e_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "repo"
        shutil.copytree(ROOT / "postgres" / "init_clean", self.root / "postgres" / "init_clean")
        (self.root / "postgres" / "baseline").mkdir(parents=True)
        shutil.copy(ROOT / "postgres" / "baseline" / "B216.json", self.root / "postgres" / "baseline")
        shutil.copy(ROOT / "Dockerfile.postgres", self.root)
        self.migs = self.root / "postgres" / "migrations"
        self.migs.mkdir(parents=True)
        self.dumps = self.tmp / "dumps"
        self.baseline = manifest.load_baseline(manifest.Layout(self.root))

    # ── helpers ──
    def psql(self, db, sql, container=None):
        p = subprocess.run(["docker", "exec", "-i", container or self.container, "psql", "-X", "-q", "-A", "-t",
                            "-v", "ON_ERROR_STOP=1", "-U", USER, "-d", db],
                           input=sql.encode(), capture_output=True)
        if p.returncode != 0:
            raise AssertionError(p.stderr.decode(errors="replace"))
        return p.stdout.decode().strip()

    def new_db(self, prefix="b03e_t"):
        name = f"{prefix}_{uuid.uuid4().hex[:8]}"
        self.psql("postgres", f"CREATE DATABASE {name} TEMPLATE {TEMPLATE};")
        self.addCleanup(self.psql, "postgres", f"DROP DATABASE IF EXISTS {name} WITH (FORCE);")
        return name

    def migrate(self, *args, db, env=None, container=None, root=None, confirm=True, popen=False):
        cmd = [sys.executable, str(ROOT / "scripts" / "migrate.py"), args[0], "--container", container or self.container,
               "--db", db, "--repo-root", str(root or self.root), *args[1:]]
        if confirm and args[0] in commands.WRITES:
            cmd.append("--confirm-isolated")
        e = {**os.environ, "PYTHONIOENCODING": "utf-8", "EDU_AI_MIGRATE_TESTING": "1", **(env or {})}
        if popen:
            return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=e)
        p = subprocess.run(cmd, capture_output=True, env=e)
        return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")

    def target(self, db):
        return pg.Target(db=db, user=USER, container=self.container)

    def has_registry(self, db):
        return self.psql(db, "SELECT to_regclass('edu_ai.schema_migration') IS NOT NULL;") == "t"

    def ids(self, db):
        return self.psql(db, "SELECT migration_id FROM edu_ai.schema_migration ORDER BY applied_at;").split()

    def has_column(self, db, col):
        return self.psql(db, "SELECT count(*) FROM information_schema.columns WHERE table_schema='edu_ai' "
                             f"AND table_name='settings' AND column_name='{col}';") == "1"

    def write_migs(self, files):
        for name, sql in files.items():
            (self.migs / name).write_bytes(sql.encode("utf-8"))

    def initialised(self, prefix="b03e_t"):
        db = self.new_db(prefix)
        rc, out = self.migrate("init", db=db)
        self.assertEqual(0, rc, out)
        return db

    def make_release(self, name, files, env=None):
        self.write_migs(files)
        ref = self.initialised("b03e_ref")
        rc, out = self.migrate("gen-release", "--release", name, db=ref, env=env)
        self.assertEqual(0, rc, out)
        return ref, json.loads((self.root / "postgres" / "releases" / f"{name}.json").read_text(encoding="utf-8"))

    def apply(self, db, release="r1", env=None):
        return self.migrate("apply", "--release", release, "--dump-dir", str(self.dumps), db=db, env=env)

    def fp(self, db):
        return fingerprint.measure_schema(self.target(db))


# ── A: baseline manifest ──────────────────────────────────────────────────────
@needs_db
class BaselineTest(Base):
    @unittest.skipUnless(C2, "second container not set")
    def test_two_independent_clean_starts_give_the_committed_manifest(self):
        outs = []
        for c in (C1, C2):
            out = self.tmp / f"{c}.json"
            rc, text = self.migrate("gen-baseline", "--out", str(out), db="edu_ai", container=c, root=ROOT)
            self.assertEqual(0, rc, text)
            outs.append(out.read_bytes())
        self.assertEqual(outs[0], outs[1])
        # Informational provenance follows the input commit, while the committed
        # B216 retains its historical generation commit. All other bytes must match.
        current = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
        generated = json.loads(outs[0])
        self.assertEqual(generated["source"]["base_commit"], current)
        expected = json.loads((ROOT / "postgres" / "baseline" / "B216.json").read_bytes())
        expected["source"]["base_commit"] = current
        self.assertEqual(outs[0], manifest.dump_json(expected).encode("utf-8"))

    def test_reference_image_is_built_on_the_pinned_digest(self):
        base = self.baseline["postgres"]["base_image"]

        def layers(ref):
            p = subprocess.run(["docker", "image", "inspect", ref, "--format", "{{json .RootFS.Layers}}"],
                               capture_output=True, text=True)
            return json.loads(p.stdout) if p.returncode == 0 else None

        image_id = subprocess.run(["docker", "inspect", self.container, "--format", "{{.Image}}"],
                                  capture_output=True, text=True).stdout.strip()
        base_layers, own = layers(base), layers(image_id)
        if base_layers is None:
            self.skipTest(f"{base} is not in the local image store")
        self.assertEqual(base_layers, own[:len(base_layers)])
        self.assertEqual(self.baseline["postgres"]["server_version"],
                         self.psql("postgres", "SELECT split_part(current_setting('server_version'), ' ', 1);"))

    def test_fingerprints_are_stable_and_match_the_manifest(self):
        db = self.new_db()
        want = self.baseline["fingerprint"]
        for _ in range(2):
            self.assertEqual(want["schema"], self.fp(db))
            self.assertEqual(want["config"], fingerprint.measure_config(self.target(db)))

    def test_init_refuses_a_changed_installation_file(self):
        f = self.root / "postgres" / "init_clean" / "01_schema.sql"
        f.write_bytes(f.read_bytes() + b"-- tampered\n")
        db = self.new_db()
        rc, out = self.migrate("init", db=db)
        self.assertEqual(1, rc, out)
        self.assertIn("01_schema.sql", out)
        self.assertFalse(self.has_registry(db))

    def test_init_refuses_a_changed_schema(self):
        db = self.new_db()
        self.psql(db, "ALTER TABLE edu_ai.settings ADD COLUMN drift int;")
        rc, out = self.migrate("init", db=db)
        self.assertEqual(1, rc, out)
        self.assertIn("schema fingerprint differs", out)
        self.assertFalse(self.has_registry(db))

    def test_init_refuses_changed_configuration(self):
        db = self.new_db()
        self.psql(db, "INSERT INTO edu_ai.qa_blocklist (term, term_norm, category, source) "
                      "VALUES ('x', 'x', 'meta', 'b03e-test');")
        rc, out = self.migrate("init", db=db)
        self.assertEqual(1, rc, out)
        self.assertIn("configuration fingerprint differs", out)
        self.assertFalse(self.has_registry(db))

    def test_init_refuses_a_database_with_content(self):
        db = self.new_db()
        self.psql(db, "INSERT INTO edu_ai.question_answer_log (raw_query) VALUES ('synthetic');")
        rc, out = self.migrate("init", db=db)
        self.assertEqual(1, rc, out)
        self.assertIn("content tables hold", out)
        self.assertFalse(self.has_registry(db))

    def test_write_commands_require_the_isolation_flag(self):
        db = self.new_db()
        for cmd in ("init", "apply", "gen-baseline", "gen-release"):
            extra = ["--dump-dir", str(self.dumps)] if cmd == "apply" else []
            rc, out = self.migrate(cmd, "--release", "r", *extra, db=db, confirm=False)
            self.assertEqual(2, rc, (cmd, out))
        self.assertFalse(self.has_registry(db))


# ── B: registry, init, status, plan ───────────────────────────────────────────
@needs_db
class RegistryTest(Base):
    def test_init_registers_one_baseline_row_and_keeps_the_fingerprint(self):
        db = self.initialised()
        self.assertEqual(["B216"], self.ids(db))
        self.assertEqual("baseline", self.psql(db, "SELECT kind FROM edu_ai.schema_migration;"))
        self.assertEqual(self.baseline["fingerprint"]["schema"],
                         self.psql(db, "SELECT checksum FROM edu_ai.schema_migration;"))
        self.assertEqual(self.baseline["fingerprint"]["schema"], self.fp(db))
        rc, out = self.migrate("status", db=db)
        self.assertEqual(0, rc, out)
        self.assertIn("schema fingerprint: matches", out)
        self.assertIn("registry definition: v1 ok", out)

    def test_init_twice_is_refused(self):
        db = self.initialised()
        rc, out = self.migrate("init", db=db)
        self.assertEqual(1, rc, out)
        self.assertIn("registry already exists", out)
        self.assertEqual(["B216"], self.ids(db))

    def test_failure_inside_init_leaves_no_table_at_all(self):
        for point in ("init:after_ddl", "init:after_insert"):
            for mode in ("error", "terminate"):
                with self.subTest(point=point, mode=mode):
                    db = self.new_db()
                    rc, out = self.migrate("init", db=db, env={"EDU_AI_MIGRATE_FAULT": f"{point}={mode}"})
                    self.assertEqual(4, rc, out)
                    self.assertIn("rolled back", out)
                    self.assertFalse(self.has_registry(db), out)
                    self.assertEqual(self.baseline["fingerprint"]["schema"], self.fp(db))
                    rc, out = self.migrate("init", db=db)  # and a retry succeeds
                    self.assertEqual(0, rc, out)

    def test_failed_verification_after_commit_keeps_the_registry_and_exits_6(self):
        db = self.new_db()
        rc, out = self.migrate("init", db=db, env={"EDU_AI_MIGRATE_FAULT": "init:after_commit=drift"})
        self.assertEqual(6, rc, out)
        self.assertIn("baseline registry is COMMITTED", out)
        self.assertIn("final verification did not succeed", out)
        self.assertIn("schema fingerprint differs", out)
        self.assertNotIn("rolled back", out)
        self.assertNotIn("dropped again", out)
        self.assertEqual(["B216"], self.ids(db))                 # the registry and its row are kept
        self.assertTrue(self.has_column(db, "fault_injected"))   # and nothing else was touched
        rc, out = self.migrate("status", db=db)
        self.assertEqual(1, rc, out)                              # status reports the drift, as it should
        self.assertIn("schema drift", out)

    def test_extra_column_in_a_business_table_is_schema_drift(self):
        db = self.initialised()
        self.psql(db, "ALTER TABLE edu_ai.settings ADD COLUMN stray int;")
        rc, out = self.migrate("status", db=db)
        self.assertEqual(1, rc, out)
        self.assertIn("schema drift", out)
        self.assertIn("registry definition: v1 ok", out)

    def test_registry_definition_drift_is_found_while_the_schema_fingerprint_is_unchanged(self):
        db = self.initialised()
        self.psql(db, "ALTER TABLE edu_ai.schema_migration ADD COLUMN stray int;")
        self.assertEqual(self.baseline["fingerprint"]["schema"], self.fp(db))
        rc, out = self.migrate("status", db=db)
        self.assertEqual(1, rc, out)
        self.assertIn("registry definition: DRIFT", out)
        self.assertIn("unexpected: column stray integer", out)
        self.assertIn("schema fingerprint: matches", out)

    def test_database_without_registry_gets_a_clear_error_and_nothing_is_converted(self):
        db = self.new_db()
        for cmd in ("status", "plan", "apply"):
            extra = ["--dump-dir", str(self.dumps)] if cmd == "apply" else []
            rc, out = self.migrate(cmd, "--release", "r1", *extra, db=db)
            self.assertEqual(1, rc, (cmd, out))
            self.assertIn("has no migration registry", out)
            self.assertIn("takeover path", out)
            self.assertFalse(self.has_registry(db), cmd)
        self.assertFalse(self.dumps.exists())

    def test_status_and_plan_are_read_only(self):
        db = self.initialised()
        snapshot = "SELECT md5(string_agg(t::text || xmin::text, ',')) FROM edu_ai.schema_migration t;"
        before = self.psql(db, snapshot)
        for cmd in ("status", "plan"):
            self.assertEqual(0, self.migrate(cmd, db=db)[0])
        self.assertEqual(before, self.psql(db, snapshot))
        with self.assertRaises(pg.PgError):  # the session option really forbids writes
            pg.run_psql(self.target(db), "INSERT INTO edu_ai.schema_migration (migration_id, kind, checksum) "
                                         "VALUES ('x', 'migration', 'y');", readonly=True)
        self.assertEqual(["B216"], self.ids(db))

    def test_expected_registry_definition_matches_the_real_ddl(self):
        db = self.initialised()
        self.assertEqual([], commands.registry.drift(commands.registry_lines(self.target(db))))

    def test_init_works_while_a_second_connection_runs_pg_dump(self):
        # init measures the fingerprint on another connection while its transaction is open
        db = self.new_db()
        rc, out = self.migrate("init", db=db)
        self.assertEqual(0, rc, out)
        self.assertEqual(self.baseline["fingerprint"]["schema"], self.fp(db))


# ── C: apply ──────────────────────────────────────────────────────────────────
@needs_db
class ApplyTest(Base):
    def test_release_manifest_apply_repeat_and_two_independent_installs(self):
        ref, rel = self.make_release("r1", {"217_add_column.sql": M217})
        self.assertEqual(["217_add_column"], [m["id"] for m in rel["migrations"]])
        entry = rel["migrations"][0]
        self.assertEqual(entry["schema_after"], self.fp(ref))
        self.assertNotEqual(entry["schema_after"], self.baseline["fingerprint"]["schema"])

        a, b = self.initialised(), self.initialised()
        rc, out = self.apply(a)
        self.assertEqual(0, rc, out)
        self.assertEqual(["B216", "217_add_column"], self.ids(a))
        self.assertEqual(entry["sha256"], self.psql(a, "SELECT checksum FROM edu_ai.schema_migration "
                                                       "WHERE kind='migration';"))
        self.assertIn("schema_after verified", self.psql(a, "SELECT notes FROM edu_ai.schema_migration "
                                                            "WHERE kind='migration';"))
        self.assertEqual(0, self.apply(b)[0])
        self.assertEqual(self.fp(a), self.fp(b))
        self.assertEqual(entry["schema_after"], self.fp(a))

        dumps = list(self.dumps.glob("*.dump"))
        self.assertEqual(2, len(dumps))
        for d in dumps:
            self.assertTrue(any("schema_migration" in l for l in pg.pg_restore_list(self.target(a), str(d))))

        before = len(list(self.dumps.glob("*.dump")))
        rc, out = self.apply(a)  # repeat: nothing to do, no new dump
        self.assertEqual(0, rc, out)
        self.assertIn("nothing to apply", out)
        self.assertEqual(before, len(list(self.dumps.glob("*.dump"))))
        rc, out = self.migrate("status", "--release", "r1", db=a)
        self.assertEqual(0, rc, out)

    def test_changed_applied_file_is_reported_by_status_plan_and_apply(self):
        self.make_release("r1", {"217_add_column.sql": M217})
        db = self.initialised()
        self.assertEqual(0, self.apply(db)[0])
        (self.migs / "217_add_column.sql").write_text(M217 + "-- edited later\n")
        for cmd in ("status", "plan"):
            rc, out = self.migrate(cmd, "--release", "r1", db=db)
            self.assertEqual(1, rc, out)
            self.assertIn("checksum drift", out)
        rc, out = self.apply(db)
        self.assertEqual(1, rc, out)
        self.assertIn("checksum drift", out)

    def test_changed_pending_file_is_refused(self):
        self.make_release("r1", {"217_add_column.sql": M217})
        (self.migs / "217_add_column.sql").write_text(M217.replace("text", "int"))
        db = self.initialised()
        rc, out = self.apply(db)
        self.assertEqual(1, rc, out)
        self.assertIn("differs from the release manifest", out)
        self.assertFalse(self.has_column(db, "b03e_note"))

    def test_manual_schema_change_before_apply_is_refused_before_anything_runs(self):
        self.make_release("r1", {"217_add_column.sql": M217})
        db = self.initialised()
        self.psql(db, "ALTER TABLE edu_ai.settings ADD COLUMN manual int;")
        rc, out = self.apply(db)
        self.assertEqual(1, rc, out)
        self.assertIn("schema drift", out)
        self.assertEqual(["B216"], self.ids(db))
        self.assertFalse(self.has_column(db, "b03e_note"))
        self.assertFalse(self.dumps.exists())

    def test_lint_refuses_a_file_that_controls_the_transaction(self):
        self.write_migs({"217_bad.sql": "ALTER TABLE edu_ai.settings ADD COLUMN c int;\nCOMMIT;\n"})
        ref = self.initialised("b03e_ref")
        rc, out = self.migrate("gen-release", "--release", "r1", db=ref)
        self.assertEqual(1, rc, out)
        self.assertIn("217_bad.sql:2:", out)
        self.assertEqual(["B216"], self.ids(ref))

    def test_error_in_the_middle_of_a_file_rolls_back_everything_and_stops_later_files(self):
        boom = ("ALTER TABLE edu_ai.settings ADD COLUMN b03e_mid int;\n"
                "DO $$ BEGIN IF current_database() NOT LIKE 'b03e_ref%' THEN RAISE EXCEPTION 'boom'; END IF; END $$;\n")
        self.make_release("r1", {"217_ok.sql": M217, "218_boom.sql": boom, "219_after.sql": M218})
        db = self.initialised()
        rc, out = self.apply(db)
        self.assertEqual(4, rc, out)
        self.assertIn("218_boom FAILED", out)
        self.assertIn("rolled back", out)
        self.assertEqual(["B216", "217_ok"], self.ids(db))
        self.assertTrue(self.has_column(db, "b03e_note"))
        self.assertFalse(self.has_column(db, "b03e_mid"))
        self.assertFalse(self.has_column(db, "b03e_note2"))
        # apply again: restarts at the failed file
        rc, out = self.apply(db)
        self.assertEqual(4, rc, out)
        self.assertEqual(["B216", "217_ok"], self.ids(db))

    def test_early_commit_in_a_file_is_caught_by_the_guard_but_cannot_be_undone(self):
        env = {"EDU_AI_MIGRATE_SKIP_LINT": "1"}
        early = ("ALTER TABLE edu_ai.settings ADD COLUMN early1 int;\nCOMMIT;\n"
                 "ALTER TABLE edu_ai.settings ADD COLUMN early2 int;\n")
        self.write_migs({"217_early.sql": early})
        sha = fingerprint.file_sha256(early.encode())
        (self.root / "postgres" / "releases").mkdir()
        (self.root / "postgres" / "releases" / "r1.json").write_text(json.dumps({
            "format": 1, "release": "r1", "baseline": "B216", "fingerprint_algorithm": fingerprint.ALGORITHM,
            "baseline_schema_fingerprint": self.baseline["fingerprint"]["schema"],
            "migrations": [{"id": "217_early", "sha256": sha, "schema_after": "0" * 64}]}))
        db = self.initialised()
        rc, out = self.apply(db, env=env)
        self.assertEqual(4, rc, out)
        self.assertIn("migration_tx guard", out)
        self.assertIn("CANNOT undo", out)
        self.assertIn("registry row for 217_early: absent", out)
        self.assertEqual(["B216"], self.ids(db))              # no registry row
        self.assertTrue(self.has_column(db, "early1"))        # what was committed stays
        self.assertTrue(self.has_column(db, "early2"))
        # without the test switch the lint refuses the same file before anything runs
        db2 = self.initialised()
        rc, out = self.apply(db2)
        self.assertEqual(1, rc, out)
        self.assertIn("[tx-control]", out)
        self.assertFalse(self.has_column(db2, "early1"))

    def test_psql_single_transaction_would_not_protect_a_file_with_commit(self):
        db = self.new_db()
        p = subprocess.run(["docker", "exec", "-i", self.container, "psql", "-X", "-q", "-1", "-v", "ON_ERROR_STOP=1",
                            "-U", USER, "-d", db],
                           input=b"ALTER TABLE edu_ai.settings ADD COLUMN early int;\nCOMMIT;\nSELECT 1/0;\n",
                           capture_output=True)
        self.assertNotEqual(0, p.returncode)
        self.assertTrue(self.has_column(db, "early"), "the change committed although -1 'failed'")

    def test_post_commit_mismatch_stops_later_migrations_and_admits_no_rollback(self):
        state_dependent = (
            "DO $$ BEGIN IF to_regclass('public.b03e_marker') IS NULL THEN\n"
            "  ALTER TABLE edu_ai.settings ADD COLUMN b03e_dep int;\n"
            "ELSE ALTER TABLE edu_ai.settings ADD COLUMN b03e_dep bigint; END IF; END $$;\n")
        self.make_release("r1", {"217_dep.sql": state_dependent, "218_next.sql": M218})
        db = self.initialised()
        self.psql(db, "CREATE TABLE public.b03e_marker (a int);")  # outside the fingerprinted schema
        rc, out = self.apply(db)
        self.assertEqual(5, rc, out)
        self.assertIn("217_dep is COMMITTED", out)
        self.assertIn("NOT rolled back", out)
        self.assertIn("restoring the local dump", out)
        self.assertEqual(["B216", "217_dep"], self.ids(db))
        self.assertTrue(self.has_column(db, "b03e_dep"))
        self.assertFalse(self.has_column(db, "b03e_note2"))
        self.assertNotIn("schema_after verified", self.psql(db, "SELECT coalesce(notes, '') FROM "
                                                                 "edu_ai.schema_migration WHERE kind='migration';"))
        rc, out = self.migrate("status", "--release", "r1", db=db)
        self.assertEqual(1, rc, out)
        self.assertIn("schema drift", out)

    def test_two_concurrent_applies_second_stops_on_the_lock(self):
        self.make_release("r1", {"217_slow.sql": "SELECT pg_sleep(12);\n"})
        db = self.initialised()
        first = self.migrate("apply", "--release", "r1", "--dump-dir", str(self.dumps), db=db, popen=True)
        try:
            for _ in range(120):
                held = self.psql("postgres", "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND granted "
                                             f"AND database=(SELECT oid FROM pg_database WHERE datname='{db}');")
                if held != "0" or first.poll() is not None:
                    break
                time.sleep(0.5)
            self.assertIsNone(first.poll(), "first apply ended before the lock was observed")
            rc, out = self.apply(db)
            self.assertEqual(3, rc, out)
            self.assertIn("another update is running", out)
            self.assertEqual(["B216"], self.ids(db))
        finally:
            text = first.communicate(timeout=180)[0].decode("utf-8", "replace")
        self.assertEqual(0, first.returncode, text)
        self.assertEqual(["B216", "217_slow"], self.ids(db))

    def test_killed_runner_rolls_back_the_open_migration_and_frees_the_lock(self):
        slow = ("ALTER TABLE edu_ai.settings ADD COLUMN b03e_kill int;\n"
                "DO $$ BEGIN IF current_database() NOT LIKE 'b03e_ref%' THEN PERFORM pg_sleep(15); END IF; END $$;\n")
        self.make_release("r1", {"217_slow.sql": slow})
        db = self.initialised()
        run = self.migrate("apply", "--release", "r1", "--dump-dir", str(self.dumps), db=db, popen=True)
        sleeping = ("SELECT count(*) FROM pg_stat_activity WHERE state = 'active' AND query LIKE '%pg_sleep(15)%' "
                    f"AND datname = '{db}';")
        try:
            for _ in range(120):
                if self.psql("postgres", sleeping) != "0" or run.poll() is not None:
                    break
                time.sleep(0.5)
            self.assertIsNone(run.poll(), "apply ended before the migration was running")
            run.kill()
        finally:
            run.communicate(timeout=60)
        busy = ("SELECT (SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted AND database = "
                f"(SELECT oid FROM pg_database WHERE datname = '{db}')) + (SELECT count(*) FROM pg_stat_activity "
                f"WHERE datname = '{db}' AND pid <> pg_backend_pid());")
        for _ in range(120):  # the open statement finishes, psql sees EOF and disconnects
            if self.psql("postgres", busy) == "0":
                break
            time.sleep(1)
        self.assertEqual("0", self.psql("postgres", busy), "session or advisory lock still held")
        self.assertEqual(["B216"], self.ids(db))
        self.assertFalse(self.has_column(db, "b03e_kill"))
        self.assertEqual(0, self.apply(db)[0])  # the lock is free again; the retry completes
        self.assertEqual(["B216", "217_slow"], self.ids(db))

    def test_data_only_migration_leaves_schema_after_unchanged_and_checks_itself(self):
        good = ("INSERT INTO edu_ai.qa_blocklist (term, term_norm, category, source)\n"
                "VALUES ('b03e', 'b03e', 'meta', 'b03e-config');\n"
                "DO $$ BEGIN IF (SELECT count(*) FROM edu_ai.qa_blocklist WHERE source = 'b03e-config') <> 1 THEN\n"
                "  RAISE EXCEPTION 'self-check: expected exactly one b03e-config row'; END IF; END $$;\n")
        ref, rel = self.make_release("r1", {"217_config.sql": good})
        self.assertEqual(self.baseline["fingerprint"]["schema"], rel["migrations"][0]["schema_after"])
        db = self.initialised()
        rc, out = self.apply(db)
        self.assertEqual(0, rc, out)
        self.assertEqual("1", self.psql(db, "SELECT count(*) FROM edu_ai.qa_blocklist WHERE source='b03e-config';"))
        # a later manual change of the data is NOT something the schema fingerprint can see
        self.psql(db, "UPDATE edu_ai.qa_blocklist SET term = 'edited' WHERE source = 'b03e-config';")
        rc, out = self.migrate("status", "--release", "r1", db=db)
        self.assertEqual(0, rc, out)
        self.assertIn("does not detect changes to rows", out)

    def test_data_only_migration_with_a_failing_self_check_rolls_back(self):
        bad = ("INSERT INTO edu_ai.qa_blocklist (term, term_norm, category, source)\n"
               "VALUES ('b03e', 'b03e', 'meta', 'b03e-config');\n"
               "DO $$ BEGIN IF current_database() NOT LIKE 'b03e_ref%' THEN\n"
               "  RAISE EXCEPTION 'self-check: unexpected state'; END IF; END $$;\n")
        self.make_release("r1", {"217_config.sql": bad})
        db = self.initialised()
        rc, out = self.apply(db)
        self.assertEqual(4, rc, out)
        self.assertEqual("0", self.psql(db, "SELECT count(*) FROM edu_ai.qa_blocklist WHERE source='b03e-config';"))
        self.assertEqual(["B216"], self.ids(db))

    def test_gen_release_needs_a_fresh_reference(self):
        self.write_migs({"217_add_column.sql": M217})
        ref = self.initialised("b03e_ref")
        self.assertEqual(0, self.migrate("gen-release", "--release", "r1", db=ref)[0])
        rc, out = self.migrate("gen-release", "--release", "r2", db=ref)
        self.assertEqual(1, rc, out)
        self.assertIn("freshly initialised B216", out)

    def test_migration_file_names_and_numbers_are_enforced(self):
        db = self.initialised()
        for name, why in (("216_old.sql", "belong to baseline"), ("Bad Name.sql", "NNN_name.sql")):
            self.write_migs({name: "SELECT 1;\n"})
            rc, out = self.migrate("gen-release", "--release", "r1", db=self.initialised("b03e_ref"))
            self.assertEqual(1, rc, out)
            self.assertIn(why, out)
            (self.migs / name).unlink()
        self.write_migs({"217_a.sql": "SELECT 1;\n", "217_b.sql": "SELECT 2;\n"})
        rc, out = self.migrate("gen-release", "--release", "r1", db=self.initialised("b03e_ref"))
        self.assertEqual(1, rc, out)
        self.assertIn("duplicate number 217", out)
        self.assertEqual(["B216"], self.ids(db))


# ── failure before COMMIT, connection lost during COMMIT, error after COMMIT ──
@needs_db
class CommitOutcomeTest(Base):
    def fault(self, spec):
        return {"EDU_AI_MIGRATE_FAULT": spec}

    def captured(self, fn, *a, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = fn(*a, **kw)
        return result, buf.getvalue()

    # init
    def test_init_connection_lost_after_commit_was_executed_is_confirmed_by_the_registry(self):
        db = self.new_db()
        rc, out = self.migrate("init", db=db, env=self.fault("init:commit=lose_ack"))
        self.assertEqual(0, rc, out)
        self.assertIn("registry confirms that the commit happened", out)
        self.assertNotIn("rolled back", out)
        self.assertEqual(["B216"], self.ids(db))
        self.assertEqual(0, self.migrate("status", db=db)[0])

    def test_init_loses_the_commit_ack_and_a_second_runner_applies_217_before_the_final_check(self):
        self.make_release("r1", {"217_add_column.sql": M217})
        db = self.new_db()
        gate = self.tmp / "gate"
        gate.mkdir()
        env = self.fault(f"init:commit=lose_ack,init:before_verify=hold:{gate}")
        init = self.migrate("init", db=db, env=env, popen=True)
        try:
            for _ in range(240):  # deterministic: init is parked after its COMMIT, before its final check
                if (gate / "reached").exists() or init.poll() is not None:
                    break
                time.sleep(0.5)
            self.assertTrue((gate / "reached").exists(), "init never reached the hold point")
            self.assertEqual(["B216"], self.ids(db))
            rc, out = self.apply(db)   # the other runner: registry is committed, init's lock is gone
            self.assertEqual(0, rc, out)
            self.assertEqual(["B216", "217_add_column"], self.ids(db))
        finally:
            (gate / "release").write_text("1")
            text = init.communicate(timeout=180)[0].decode("utf-8", "replace")
        self.assertEqual(6, init.returncode, text)
        self.assertIn("baseline registry is COMMITTED", text)
        self.assertIn("final verification did not succeed", text)
        self.assertIn("another runner applied a migration", text)
        self.assertIn("Nothing was dropped", text)
        self.assertNotIn("rolled back", text)
        self.assertEqual(["B216", "217_add_column"], self.ids(db))   # both valid rows survive
        self.assertEqual(["baseline", "migration"],
                         self.psql(db, "SELECT kind FROM edu_ai.schema_migration ORDER BY applied_at;").split())
        self.assertEqual(self.baseline["fingerprint"]["schema"],
                         self.psql(db, "SELECT checksum FROM edu_ai.schema_migration WHERE kind='baseline';"))
        rc, out = self.migrate("status", "--release", "r1", db=db)
        self.assertEqual(0, rc, out)                                  # the database is consistent

    def test_init_connection_lost_before_commit_reports_no_commit_not_a_confirmed_state(self):
        db = self.new_db()
        rc, out = self.migrate("init", db=db, env=self.fault("init:commit=lose_before"))
        self.assertEqual(4, rc, out)
        self.assertIn("did not commit", out)
        self.assertNotIn("confirms that the commit happened", out)
        self.assertFalse(self.has_registry(db))

    def test_init_error_after_commit_does_not_claim_a_rollback_or_drop_anything(self):
        db = self.new_db()
        rc, out = self.migrate("init", db=db, env=self.fault("init:after_commit=error"))
        self.assertEqual(6, rc, out)
        self.assertIn("COMMITTED", out)
        self.assertNotIn("rolled back", out)
        self.assertIn("Nothing was dropped", out)
        self.assertTrue(self.has_registry(db))

    # apply
    def test_apply_failure_before_commit_is_a_confirmed_rollback(self):
        boom = "ALTER TABLE edu_ai.settings ADD COLUMN b03e_pre int;\nSELECT 1/0;\n"
        self.write_migs({"217_boom.sql": boom})
        db = self.initialised()
        self.write_release_manifest("r1", [("217_boom", boom)])
        rc, out = self.apply(db)
        self.assertEqual(4, rc, out)
        self.assertIn("FAILED before COMMIT", out)
        self.assertIn("registry row for 217_boom: absent", out)
        self.assertIn("rolled back", out)
        self.assertFalse(self.has_column(db, "b03e_pre"))

    def test_apply_connection_lost_after_commit_executed_stops_and_says_committed(self):
        self.make_release("r1", {"217_a.sql": M217, "218_b.sql": M218})
        db = self.initialised()
        rc, out = self.apply(db, env=self.fault("apply:commit=lose_ack"))
        self.assertEqual(6, rc, out)
        self.assertIn("217_a is COMMITTED", out)
        self.assertIn("not a rollback", out)
        self.assertNotIn("rolled back", out.replace("not a rollback", ""))
        self.assertEqual(["B216", "217_a"], self.ids(db))
        self.assertFalse(self.has_column(db, "b03e_note2"))
        self.assertEqual(0, self.migrate("status", "--release", "r1", db=db)[0])
        rc, out = self.apply(db)  # the lock is free; the run simply continues
        self.assertEqual(0, rc, out)
        self.assertEqual(["B216", "217_a", "218_b"], self.ids(db))

    def test_apply_connection_lost_before_commit_is_not_committed(self):
        self.make_release("r1", {"217_a.sql": M217})
        db = self.initialised()
        rc, out = self.apply(db, env=self.fault("apply:commit=lose_before"))
        self.assertEqual(4, rc, out)
        self.assertIn("while COMMIT was in flight", out)
        self.assertIn("registry row for 217_a: absent", out)
        self.assertIn("the COMMIT did not happen", out)
        self.assertNotIn("COMMITTED", out)
        self.assertEqual(["B216"], self.ids(db))
        self.assertFalse(self.has_column(db, "b03e_note"))
        self.assertEqual(0, self.apply(db)[0])

    def test_apply_error_after_commit_says_committed_and_never_rolled_back(self):
        self.make_release("r1", {"217_a.sql": M217, "218_b.sql": M218})
        db = self.initialised()
        rc, out = self.apply(db, env=self.fault("apply:after_commit=error"))
        self.assertEqual(6, rc, out)
        self.assertIn("217_a is COMMITTED", out)
        self.assertNotIn("rolled back", out.replace("not a rollback", ""))
        self.assertEqual(["B216", "217_a"], self.ids(db))
        self.assertFalse(self.has_column(db, "b03e_note2"))

    def test_unknown_outcome_is_reported_as_unknown(self):
        self.make_release("r1", {"217_a.sql": M217})
        db = self.initialised()
        argv = ["apply", "--container", self.container, "--db", db, "--user", USER, "--repo-root", str(self.root),
                "--release", "r1", "--dump-dir", str(self.dumps), "--confirm-isolated"]
        env = {"EDU_AI_MIGRATE_TESTING": "1", "EDU_AI_MIGRATE_FAULT": "apply:commit=lose_ack"}
        with mock.patch.dict(os.environ, env), mock.patch.object(commands, "resolve_outcome", return_value="unknown"):
            rc, out = self.captured(commands.main, argv)
        self.assertEqual(6, rc, out)
        self.assertIn("UNKNOWN", out)
        self.assertNotIn("rolled back", out.replace("Do not assume a rollback", ""))
        self.assertEqual(["B216", "217_a"], self.ids(db))  # in truth it had committed

    def test_resolve_outcome_waits_for_the_dead_session_and_never_guesses(self):
        db = self.initialised()
        target = self.target(db)
        holder = pg.Session(target)
        try:
            self.assertTrue(commands.try_lock(holder))
            self.assertEqual("unknown", commands.resolve_outcome(target, lambda: True, wait=1.5))
        finally:
            holder.close()
        self.assertEqual("committed", commands.resolve_outcome(target, lambda: True))
        self.assertEqual("not_committed", commands.resolve_outcome(target, lambda: False))

        def unreadable():
            raise pg.PgError("registry unreadable")

        self.assertEqual("unknown", commands.resolve_outcome(target, unreadable))

    def test_failure_text_claims_no_rollback_when_the_registry_cannot_be_read(self):
        failed = commands.MigrationFailed("217_x", "boom", guard=False)
        with mock.patch.object(commands, "registry_rows", side_effect=pg.PgError("down")):
            _, out = self.captured(commands.explain_failure, self.target("edu_ai"), failed, None)
        self.assertNotIn("rolled back", out)
        self.assertIn("no rollback is claimed", out)
        with mock.patch.object(commands, "registry_rows", return_value=[{"id": "217_x"}]):
            _, out = self.captured(commands.explain_failure, self.target("edu_ai"), failed, None)
        self.assertNotIn("rolled back", out)
        self.assertIn("state is unclear", out)

    def write_release_manifest(self, name, items):
        (self.root / "postgres" / "releases").mkdir(exist_ok=True)
        (self.root / "postgres" / "releases" / f"{name}.json").write_text(json.dumps({
            "format": 1, "release": name, "baseline": "B216", "fingerprint_algorithm": fingerprint.ALGORITHM,
            "baseline_schema_fingerprint": self.baseline["fingerprint"]["schema"],
            "migrations": [{"id": i, "sha256": fingerprint.file_sha256(t.encode()),
                            "schema_after": self.baseline["fingerprint"]["schema"]} for i, t in items]}))


# ── C: the pre-apply dump is judged by pg_dump and by reading the archive ─────
@needs_db
class DumpTest(Base):
    def test_garbage_archive_with_a_nonzero_size_is_rejected(self):
        db = self.initialised()

        def garbage(target, path):
            Path(path).write_bytes(b"this is not a pg_dump archive\n" * 100)

        with mock.patch.object(pg, "pg_dump_file", garbage):
            with self.assertRaises(pg.PgError):
                commands.take_dump(self.target(db), self.dumps, "217_x")
        self.assertEqual([], list(self.dumps.glob("*")))

    def test_failing_pg_dump_is_rejected(self):
        with self.assertRaises(pg.PgError):
            commands.take_dump(self.target("b03e_no_such_database"), self.dumps, "217_x")
        self.assertEqual([], list(self.dumps.glob("*")))

    def test_apply_refuses_to_run_without_a_usable_dump(self):
        self.make_release("r1", {"217_add_column.sql": M217})
        db = self.initialised()

        def garbage(target, path):
            Path(path).write_bytes(b"x" * 4096)

        out = io.StringIO()
        argv = ["apply", "--container", self.container, "--db", db, "--user", USER, "--repo-root", str(self.root),
                "--release", "r1", "--dump-dir", str(self.dumps), "--confirm-isolated"]
        with mock.patch.object(pg, "pg_dump_file", garbage), contextlib.redirect_stdout(out):
            rc = commands.main(argv)
        self.assertEqual(1, rc, out.getvalue())
        self.assertIn("dump is not usable", out.getvalue())
        self.assertEqual(["B216"], self.ids(db))
        self.assertFalse(self.has_column(db, "b03e_note"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
