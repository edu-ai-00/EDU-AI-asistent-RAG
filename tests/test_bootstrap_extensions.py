"""Offline: bootstrap updates vector/pg_search to the image's versions only after a passing smoke test. No Docker."""
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import bootstrap  # noqa: E402


def done(rc=0, out=""):
    return subprocess.CompletedProcess([], rc, out, "")


class FakePostgres:
    """Answers the version query from `versions` and applies ALTER EXTENSION like PostgreSQL would."""

    def __init__(self, versions, alter_rc=0, probe_rc=0, alter_hangs=False):
        self.versions = dict(versions)     # name -> (catalog, image)
        self.alter_rc = alter_rc
        self.probe_rc = probe_rc
        self.alter_hangs = alter_hangs
        self.calls = []
        self.probes = []

    def __call__(self, args, **kwargs):
        if bootstrap.smoke.SEARCH_USER in args:      # the search-role probe
            self.probes.append(args)
            return done(self.probe_rc)
        sql = kwargs.get("input", "")
        self.calls.append(sql)
        if "ALTER EXTENSION" in sql and self.alter_hangs:
            raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))
        if "ALTER EXTENSION" in sql:
            if self.alter_rc == 0:
                for name, (_, image) in self.versions.items():
                    if f'ALTER EXTENSION "{name}" UPDATE' in sql:
                        self.versions[name] = (image, image)
            return done(self.alter_rc)
        return done(0, "".join(f"{n} {c} {i}\n" for n, (c, i) in sorted(self.versions.items())))

    def alters(self):
        return [c for c in self.calls if "ALTER EXTENSION" in c]


class StepExtensions(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(bootstrap, "_failed", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def step(self, pg, smoke_passed=True):
        with mock.patch.object(bootstrap, "run", pg), mock.patch("builtins.print") as out:
            bootstrap.step_extensions(smoke_passed)
        return "\n".join(str(c.args[0]) for c in out.call_args_list if c.args)

    def test_without_a_passing_smoke_nothing_is_touched(self):
        pg = FakePostgres({"vector": ("0.7.4", "0.8.7")})
        text = self.step(pg, smoke_passed=False)
        self.assertEqual([], pg.calls)
        self.assertIn("[skip]", text)
        self.assertFalse(bootstrap._failed)

    def test_matching_versions_change_nothing(self):
        pg = FakePostgres({"pg_search": ("0.25.11", "0.25.11"), "vector": ("0.8.7", "0.8.7")})
        text = self.step(pg)
        self.assertEqual([], pg.alters())
        self.assertIn("extensions match the image: pg_search 0.25.11, vector 0.8.7", text)

    def test_older_catalog_versions_are_updated_in_one_transaction(self):
        pg = FakePostgres({"pg_search": ("0.25.1", "0.25.11"), "vector": ("0.7.4", "0.8.7")})
        text = self.step(pg)
        [sql] = pg.alters()
        self.assertTrue(sql.startswith("BEGIN;\nSET LOCAL lock_timeout = '5s';\n"
                                       'ALTER EXTENSION "pg_search" UPDATE;\nALTER EXTENSION "vector" UPDATE;\n'))
        self.assertTrue(sql.endswith("\nCOMMIT;\n"))
        # The functions an update adds get the contract's privileges in the same transaction.
        self.assertIn("REVOKE ALL ON ALL ROUTINES IN SCHEMA paradedb, pdb FROM PUBLIC;", sql)
        self.assertIn("GRANT EXECUTE ON ALL ROUTINES IN SCHEMA paradedb, pdb TO edu_ai_search, edu_ai_ingest, edu_ai_owner;", sql)
        self.assertIn("pg_search 0.25.1 -> 0.25.11", text)
        self.assertIn("vector 0.7.4 -> 0.8.7", text)
        self.assertFalse(bootstrap._failed)

    def test_versions_compare_numerically(self):
        pg = FakePostgres({"pg_search": ("0.25.9", "0.25.11")})
        self.step(pg)
        self.assertEqual(1, len(pg.alters()))

    def test_a_newer_catalog_is_left_alone(self):
        pg = FakePostgres({"vector": ("0.8.7", "0.7.4")})
        text = self.step(pg)
        self.assertEqual([], pg.alters())
        self.assertIn("newer than 0.7.4 in the image", text)
        self.assertFalse(bootstrap._failed)

    def test_an_unparsable_version_is_left_alone(self):
        pg = FakePostgres({"vector": ("0.8.7-dev", "0.8.7")})
        text = self.step(pg)
        self.assertEqual([], pg.alters())
        self.assertIn("cannot compare", text)

    def test_a_failed_update_fails_the_run(self):
        pg = FakePostgres({"vector": ("0.7.4", "0.8.7")}, alter_rc=1)
        text = self.step(pg)
        self.assertTrue(bootstrap._failed)
        self.assertIn("nothing was changed", text)

    def test_an_update_that_did_not_take_fails_the_run(self):
        pg = FakePostgres({"vector": ("0.7.4", "0.8.7")})
        real = pg.__call__

        def no_effect(args, **kwargs):
            if "ALTER EXTENSION" in kwargs.get("input", ""):
                pg.calls.append(kwargs["input"])
                return done(0)
            return real(args, **kwargs)
        with mock.patch.object(bootstrap, "run", no_effect), mock.patch("builtins.print"):
            bootstrap.step_extensions(True)
        self.assertTrue(bootstrap._failed)

    def test_the_update_does_not_wait_behind_the_application(self):
        pg = FakePostgres({"vector": ("0.7.4", "0.8.7")})
        self.step(pg)
        self.assertIn("BEGIN;\nSET LOCAL lock_timeout = '5s';\nALTER EXTENSION", pg.alters()[0])

    def test_search_is_probed_after_an_update_only(self):
        pg = FakePostgres({"vector": ("0.8.7", "0.8.7")})
        self.step(pg)
        self.assertEqual([], pg.probes)
        pg = FakePostgres({"vector": ("0.7.4", "0.8.7")})
        text = self.step(pg)
        self.assertEqual(1, len(pg.probes))
        self.assertIn("BM25 and vector search work for edu_ai_search after the update", text)
        self.assertFalse(bootstrap._failed)

    def test_a_failing_search_probe_fails_the_run(self):
        pg = FakePostgres({"vector": ("0.7.4", "0.8.7")}, probe_rc=1)
        self.step(pg)
        self.assertTrue(bootstrap._failed)

    def test_a_timeout_is_reported_as_unknown_not_as_a_traceback(self):
        pg = FakePostgres({"vector": ("0.7.4", "0.8.7")}, alter_hangs=True)
        text = self.step(pg)
        self.assertTrue(bootstrap._failed)
        self.assertIn("timed out; its outcome is unknown", text)
        self.assertEqual([], pg.probes)

    def test_an_unreadable_matrix_changes_nothing(self):
        pg = FakePostgres({"vector": ("0.7.4", "0.8.7")})
        with mock.patch.object(bootstrap.db_roles, "load_matrix", side_effect=bootstrap.db_roles.ContractError("bad")):
            self.step(pg)
        self.assertEqual([], pg.alters())
        self.assertTrue(bootstrap._failed)

    def test_an_unreadable_catalog_fails_the_run(self):
        with mock.patch.object(bootstrap, "run", lambda args, **kw: done(2)), mock.patch("builtins.print"):
            bootstrap.step_extensions(True)
        self.assertTrue(bootstrap._failed)

    def test_main_runs_the_step_only_after_the_smoke(self):
        src = pathlib.Path(bootstrap.__file__).read_text(encoding="utf-8")
        main = src[src.index("def main()"):]
        self.assertLess(main.index("step_smoke(base_url)"), main.index("step_extensions("))
        self.assertIn("step_extensions(smoke_passed=not args.skip_smoke and not _failed)", main)


class MajorVersionGuard(unittest.TestCase):
    CONFIG = ('{"services": {"postgres": {"volumes": [{"type": "volume", "source": "postgres_data", '
              '"target": "/var/lib/postgresql/data"}]}}, "volumes": {"postgres_data": {"name": "edu_ai_postgres_data"}}}')

    def setUp(self):
        patcher = mock.patch.object(bootstrap, "_failed", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def guard(self, pg_version, volume_exists=True, want="18"):
        calls = []

        def fake(args, **kw):
            calls.append(args)
            if args[:3] == ["docker", "compose", "config"]:
                return done(0, self.CONFIG)
            if args[:3] == ["docker", "volume", "inspect"]:
                return done(0 if volume_exists else 1)
            if args[:2] == ["docker", "run"]:
                return done(0, pg_version)
            raise AssertionError(args)
        with mock.patch.object(bootstrap, "run", fake), mock.patch.object(bootstrap, "_dockerfile_pg_major", return_value=want), \
                mock.patch("builtins.print") as out:
            bootstrap._check_pg_major()
        return calls, "\n".join(str(c.args[0]) for c in out.call_args_list if c.args)

    def test_old_major_data_is_refused_before_anything_starts(self):
        calls, text = self.guard("16\n")
        self.assertTrue(bootstrap._failed)
        self.assertIn("edu_ai_postgres_data holds PostgreSQL 16 data", text)
        self.assertIn("A new PostgreSQL major version", text)
        self.assertIn("edu_ai_postgres_data:/data:ro", calls[-1])      # read-only, the resolved volume name

    def test_same_major_empty_or_missing_volume_pass(self):
        for version, exists in (("18\n", True), ("", True), ("", False)):
            with self.subTest(version=version, exists=exists):
                bootstrap._failed = False
                self.guard(version, volume_exists=exists)
                self.assertFalse(bootstrap._failed)

    def test_dockerfile_major_is_read_from_the_base_tag(self):
        text = "# c\nFROM pgvector/pgvector:0.8.7-pg18@sha256:" + "a" * 64 + "\nENV X=1\n"
        with tempfile.TemporaryDirectory() as tmp:
            (pathlib.Path(tmp) / "Dockerfile.postgres").write_text(text, encoding="utf-8")
            with mock.patch.object(bootstrap, "ROOT", pathlib.Path(tmp)):
                self.assertEqual("18", bootstrap._dockerfile_pg_major())
        self.assertEqual("18", bootstrap._dockerfile_pg_major())             # this checkout


class WizardMajorVersionReason(unittest.TestCase):
    MODEL = {"services": {"postgres": {"volumes": [
                {"type": "volume", "source": "postgres_root", "target": "/var/lib/postgresql"},
                {"type": "volume", "source": "postgres_data", "target": "/var/lib/postgresql/data"}]}},
             "volumes": {"postgres_data": {"name": "edu_ai_postgres_data"}}}

    def change(self, pg_version, rc=0):
        import setup
        calls = []

        class FakeHost:
            def run(self, argv, **kw):
                calls.append(argv)
                return setup.Proc(rc, pg_version, "")
        layout = mock.Mock(source_root=pathlib.Path(bootstrap.ROOT), project="edu_ai")
        return setup._postgres_major_change(FakeHost(), layout, self.MODEL), calls

    def test_old_data_names_the_volume_and_both_versions(self):
        result, calls = self.change("16\n")
        self.assertEqual(("edu_ai_postgres_data", "16", "18"), result)
        self.assertIn("edu_ai_postgres_data:/data:ro", calls[0])

    def test_same_major_empty_or_unreadable_is_no_finding(self):
        for version, rc in (("18\n", 0), ("", 0), ("16\n", 1)):
            with self.subTest(version=version, rc=rc):
                self.assertIsNone(self.change(version, rc)[0])


class SmokeSearchProbe(unittest.TestCase):
    def test_probe_runs_bm25_score_and_vector_distance_as_the_search_role(self):
        args = bootstrap.smoke.search_index_probe_args("edu_ai")
        self.assertEqual(["-U", "edu_ai_search", "-d", "edu_ai"], args[args.index("-U"):args.index("-d") + 2])
        self.assertIn("ON_ERROR_STOP=1", args)
        sql = " ".join(args)
        for needle in ("@@@", "paradedb.score(", "<=>"):
            self.assertIn(needle, sql)


if __name__ == "__main__":
    unittest.main()
