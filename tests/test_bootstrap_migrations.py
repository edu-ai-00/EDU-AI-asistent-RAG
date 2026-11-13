"""Offline: bootstrap step 4b runs the migration runner on a database without school data only. No Docker.

    python3 tests/test_bootstrap_migrations.py
"""
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


class FakeStack:
    """Answers the registry/content query, `compose ps` and migrate.py calls."""

    def __init__(self, registry=False, content=0, init_rc=0, apply_rc=0, facts_rc=0):
        self.registry, self.content = registry, content
        self.init_rc, self.apply_rc, self.facts_rc = init_rc, apply_rc, facts_rc
        self.migrate = []

    def __call__(self, args, **kwargs):
        if "migrate.py" in " ".join(map(str, args)):
            cmd = args[2]
            self.migrate.append(args[2:])
            return done(self.init_rc if cmd == "init" else self.apply_rc, f"{cmd} output\n")
        if args[:4] == ["docker", "compose", "ps", "-q"]:
            return done(0, "c0ffee\n")
        if "schema_migration" in kwargs.get("input", ""):
            return done(self.facts_rc, f"{'t' if self.registry else 'f'} {self.content}\n")
        raise AssertionError(f"unexpected command {args}")

    def commands(self):
        return [m[0] for m in self.migrate]


class StepMigrations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for target, value in (("_failed", False), ("STATE_DIR", pathlib.Path(self.tmp.name))):
            patcher = mock.patch.object(bootstrap, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def step(self, stack):
        with mock.patch.object(bootstrap, "run", stack), mock.patch("builtins.print") as out:
            bootstrap.step_migrations()
        return "\n".join(str(c.args[0]) for c in out.call_args_list if c.args)

    def test_database_with_school_data_is_never_touched(self):
        # production: no registry, data present — an update must pass without the runner
        stack = FakeStack(registry=False, content=4442)
        text = self.step(stack)
        self.assertEqual([], stack.migrate)
        self.assertIn("[skip]", text)
        self.assertIn("manual procedure", text)
        self.assertFalse(bootstrap._failed)

    def test_registered_database_with_data_is_never_touched(self):
        stack = FakeStack(registry=True, content=1)
        self.step(stack)
        self.assertEqual([], stack.migrate)
        self.assertFalse(bootstrap._failed)

    def test_clean_install_is_registered_and_brought_to_the_latest_release(self):
        stack = FakeStack(registry=False, content=0)
        text = self.step(stack)
        self.assertEqual(["init", "apply"], stack.commands())
        init, apply = stack.migrate
        for call in (init, apply):
            self.assertIn("--confirm-isolated", call)
            self.assertEqual("c0ffee", call[call.index("--container") + 1])
        self.assertEqual(bootstrap._latest_release(), apply[apply.index("--release") + 1])
        dumps = pathlib.Path(apply[apply.index("--dump-dir") + 1])
        self.assertEqual(pathlib.Path(self.tmp.name) / bootstrap.MIGRATION_DUMPS, dumps)
        self.assertTrue(dumps.is_dir())
        self.assertIn("[ok  ] database at release", text)
        self.assertFalse(bootstrap._failed)

    def test_a_rerun_on_a_registered_clean_database_only_applies(self):
        stack = FakeStack(registry=True, content=0)
        self.step(stack)
        self.assertEqual(["apply"], stack.commands())
        self.assertFalse(bootstrap._failed)

    def test_refused_init_fails_and_applies_nothing(self):
        stack = FakeStack(registry=False, content=0, init_rc=1)
        text = self.step(stack)
        self.assertEqual(["init"], stack.commands())
        self.assertIn("[fail]", text)
        self.assertTrue(bootstrap._failed)

    def test_failed_apply_fails(self):
        stack = FakeStack(registry=True, content=0, apply_rc=4)
        self.step(stack)
        self.assertTrue(bootstrap._failed)

    def test_unreadable_state_fails_without_running_the_runner(self):
        stack = FakeStack(facts_rc=2)
        self.step(stack)
        self.assertEqual([], stack.migrate)
        self.assertTrue(bootstrap._failed)

    def test_without_a_release_manifest_nothing_runs(self):
        stack = FakeStack()
        with mock.patch.object(bootstrap, "ROOT", pathlib.Path(self.tmp.name)):
            text = self.step(stack)
        self.assertEqual([], stack.migrate)
        self.assertIn("[skip]", text)
        self.assertFalse(bootstrap._failed)


class LatestRelease(unittest.TestCase):
    def test_numeric_order_and_the_repository_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            rel = pathlib.Path(tmp) / "postgres" / "releases"
            rel.mkdir(parents=True)
            for name in ("R99.json", "R224.json", "R30.json", "notes.json"):
                (rel / name).write_text("{}")
            with mock.patch.object(bootstrap, "ROOT", pathlib.Path(tmp)):
                self.assertEqual("R224", bootstrap._latest_release())
        # the release bootstrap picks must list every migration file on disk
        root = pathlib.Path(__file__).resolve().parents[1]
        import json
        latest = json.loads((root / "postgres" / "releases" / f"{bootstrap._latest_release()}.json").read_text())
        on_disk = sorted(p.stem for p in (root / "postgres" / "migrations").glob("[0-9]*.sql"))
        self.assertEqual(on_disk, [m["id"] for m in latest["migrations"]])


class MainOrder(unittest.TestCase):
    def test_a_failed_migration_stops_before_the_rest_of_the_stack(self):
        calls = []

        def migrations():
            calls.append("migrations")
            bootstrap._failed = True

        with mock.patch.object(bootstrap, "_failed", False), \
                mock.patch.object(sys, "argv", ["bootstrap.py", "--no-input"]), \
                mock.patch.object(bootstrap, "_apply_layout", return_value=True), \
                mock.patch.object(bootstrap, "step_preflight"), mock.patch.object(bootstrap, "step_env"), \
                mock.patch.object(bootstrap, "step_dirs"), \
                mock.patch.object(bootstrap, "step_up", side_effect=lambda *a, **k: calls.append("up") or True), \
                mock.patch.object(bootstrap, "step_migrations", side_effect=migrations), \
                mock.patch.object(bootstrap, "step_api_key", side_effect=AssertionError("must not run")), \
                mock.patch("builtins.print"):
            self.assertEqual(1, bootstrap.main())
        self.assertEqual(["up", "migrations"], calls)


if __name__ == "__main__":
    unittest.main()
