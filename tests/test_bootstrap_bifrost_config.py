"""Offline: bootstrap copies bifrost/config.json into bifrost_data and restarts Bifrost only when the running
container started before the file changed. No Docker."""
import pathlib
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import bootstrap  # noqa: E402

CONFIG = b'{"client": {"enforce_auth_on_inference": false}}\n'


def done(rc=0, out=""):
    return subprocess.CompletedProcess([], rc, out, "denied" if rc else "")


class PlaceBifrostConfig(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "bifrost").mkdir()
        (self.root / "bifrost" / "config.json").write_bytes(CONFIG)
        (self.root / "bifrost_data").mkdir()
        for name, value in (("ROOT", self.root), ("STATE_DIR", self.root), ("_failed", False)):
            patcher = mock.patch.object(bootstrap, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.target = self.root / "bifrost_data" / "config.json"

    def place(self, platform, answer=None):
        with mock.patch.object(bootstrap.sys, "platform", platform), \
                mock.patch.object(bootstrap, "run", return_value=answer or done(0, "COPIED\n")) as run, \
                mock.patch.object(bootstrap, "report") as report, mock.patch.object(bootstrap, "fail") as fail:
            bootstrap._place_bifrost_config()
        return run, report, fail

    def test_identical_content_is_skipped_without_docker(self):
        self.target.write_bytes(CONFIG)
        run, report, fail = self.place("linux")
        run.assert_not_called(); fail.assert_not_called()
        self.assertEqual(report.call_args.args[0], "skip")

    def test_linux_copies_only_when_the_container_finds_a_difference(self):
        run, report, fail = self.place("linux")
        fail.assert_not_called()
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ["docker", "run", "--rm"])
        script = argv[-1]
        self.assertIn("; if cmp -s /src/config.json /mnt/config.json; then echo SAME;", script)
        for part in ("chown 1000:1000", "chmod 0640", "mv /mnt/.config.json.edu_ai /mnt/config.json"):
            self.assertIn(part, script)
        self.assertEqual(report.call_args.args[0], "ok")

    def test_an_unreadable_but_identical_file_is_reported_as_skipped(self):
        run, report, fail = self.place("linux", done(0, "SAME\n"))
        fail.assert_not_called()
        self.assertEqual(report.call_args.args[0], "skip")

    def test_a_failed_copy_fails(self):
        run, report, fail = self.place("linux", done(1))
        fail.assert_called_once()

    def test_a_directory_in_place_of_the_file_is_refused(self):
        run, report, fail = self.place("linux", done(3, "ISDIR\n"))
        fail.assert_called_once()
        self.assertIn("is a directory", fail.call_args.args[0])
        self.assertTrue(run.call_args.args[0][-1].startswith("if [ -d /mnt/config.json ]; then echo ISDIR; exit 3; fi; "))

    def test_elsewhere_the_file_is_written_directly(self):
        run, report, fail = self.place("darwin")
        run.assert_not_called(); fail.assert_not_called()
        self.assertEqual(self.target.read_bytes(), CONFIG)

    def test_elsewhere_a_write_error_is_reported_not_raised(self):
        with mock.patch.object(bootstrap, "write_atomic", side_effect=PermissionError()):
            run, report, fail = self.place("darwin")
        fail.assert_called_once()

    def test_a_missing_checkout_copy_fails_cleanly(self):
        (self.root / "bifrost" / "config.json").unlink()
        run, report, fail = self.place("linux")
        fail.assert_called_once()
        run.assert_not_called()


class BifrostConfigNewer(unittest.TestCase):
    STARTED = "2026-10-12T19:46:20.612644336Z"          # Docker's own format: nanoseconds
    EPOCH = int(datetime(2026, 10, 12, 19, 46, 20, tzinfo=timezone.utc).timestamp())

    def newer(self, mtime, *, ps="abc123\n", ps_rc=0, inspect_rc=0, stat_rc=0, warned=False):
        answers = {"inspect": done(inspect_rc, self.STARTED + "\n"), "exec": done(stat_rc, f"{mtime}\n")}
        with mock.patch.object(bootstrap, "compose", return_value=done(ps_rc, ps)), \
                mock.patch.object(bootstrap, "run", side_effect=lambda argv, **kw: answers[argv[1]]), \
                mock.patch.object(bootstrap, "report") as report:
            result = bootstrap._bifrost_config_newer()
        self.assertEqual(any(c.args[0] == "warn" for c in report.call_args_list), warned)
        return result

    def test_a_file_changed_after_the_start_needs_a_restart(self):
        self.assertTrue(self.newer(self.EPOCH + 60))

    def test_a_file_older_than_the_start_does_not(self):
        self.assertFalse(self.newer(self.EPOCH - 60))

    def test_unknown_states_do_not_restart_but_warn(self):
        self.assertFalse(self.newer(self.EPOCH + 60, ps="", warned=True))
        self.assertFalse(self.newer(self.EPOCH + 60, ps_rc=1, warned=True))
        self.assertFalse(self.newer(self.EPOCH + 60, inspect_rc=1, warned=True))
        self.assertFalse(self.newer(self.EPOCH + 60, stat_rc=1, warned=True))
        self.assertFalse(self.newer("not-a-number", warned=True))

    def test_no_docker_at_all_does_not_raise(self):
        with mock.patch.object(bootstrap, "compose", side_effect=FileNotFoundError()), \
                mock.patch.object(bootstrap, "report") as report:
            self.assertFalse(bootstrap._bifrost_config_newer())
        self.assertEqual(report.call_args.args[0], "warn")


class StepUpRestart(unittest.TestCase):
    def step_up(self, services, newer):
        with mock.patch.object(bootstrap, "_check_optional_cloudflare", return_value=True),                 mock.patch.object(bootstrap, "compose", return_value=done(0)),                 mock.patch.object(bootstrap, "_bifrost_config_newer", return_value=newer) as check,                 mock.patch.object(bootstrap, "_restart_bifrost", return_value=True) as restart,                 mock.patch.object(bootstrap, "report"), mock.patch.object(bootstrap, "phase"):
            self.assertTrue(bootstrap.step_up(services, "label"))
        return check, restart

    def test_bifrost_is_restarted_only_where_it_is_brought_up_and_its_config_is_newer(self):
        for services, newer, restarted in ((["postgres", "bifrost"], True, True), ([], True, True),
                                           (["postgres", "bifrost"], False, False), (["edu_ai"], True, False)):
            with self.subTest(services=services, newer=newer):
                check, restart = self.step_up(services, newer)
                self.assertEqual(restart.called, restarted)
                if services == ["edu_ai"]:
                    check.assert_not_called()


class RestartBifrost(unittest.TestCase):
    def test_restart_uses_compose_and_reports_failure(self):
        for rc, expected in ((0, True), (1, False)):
            with self.subTest(rc=rc), mock.patch.object(bootstrap, "compose", return_value=done(rc)) as compose, \
                    mock.patch.object(bootstrap, "report"), mock.patch.object(bootstrap, "fail") as fail, \
                    mock.patch.object(bootstrap, "_failed", False):
                self.assertIs(bootstrap._restart_bifrost(), expected)
                self.assertEqual(compose.call_args.args, ("restart", "bifrost"))
                self.assertEqual(fail.called, not expected)


if __name__ == "__main__":
    unittest.main()
