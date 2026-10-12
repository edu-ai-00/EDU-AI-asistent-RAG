"""Offline: bootstrap copies bifrost/config.json into bifrost_data and restarts Bifrost when it changed. No Docker."""
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import bootstrap  # noqa: E402

CONFIG = b'{"client": {"enforce_auth_on_inference": false}}\n'


class PlaceBifrostConfig(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "bifrost").mkdir()
        (self.root / "bifrost" / "config.json").write_bytes(CONFIG)
        (self.root / "bifrost_data").mkdir()
        for name, value in (("ROOT", self.root), ("STATE_DIR", self.root), ("_bifrost_config_changed", False),
                            ("_failed", False)):
            patcher = mock.patch.object(bootstrap, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.target = self.root / "bifrost_data" / "config.json"

    def place(self, platform, run_rc=0):
        done = subprocess.CompletedProcess([], run_rc, "", "denied" if run_rc else "")
        with mock.patch.object(bootstrap.sys, "platform", platform), \
                mock.patch.object(bootstrap, "run", return_value=done) as run, \
                mock.patch.object(bootstrap, "report") as report, mock.patch.object(bootstrap, "fail") as fail:
            bootstrap._place_bifrost_config()
        return run, report, fail

    def test_identical_content_is_skipped_without_docker_or_restart(self):
        self.target.write_bytes(CONFIG)
        run, report, fail = self.place("linux")
        run.assert_not_called(); fail.assert_not_called()
        self.assertEqual(report.call_args.args[0], "skip")
        self.assertFalse(bootstrap._bifrost_config_changed)

    def test_linux_copies_through_a_throwaway_container_and_marks_a_restart(self):
        run, report, fail = self.place("linux")
        fail.assert_not_called()
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ["docker", "run", "--rm"])
        script = argv[-1]
        self.assertIn("chown 1000:1000", script)
        self.assertIn("chmod 0640", script)
        self.assertIn("mv /mnt/.config.json.edu_ai /mnt/config.json", script)
        self.assertTrue(bootstrap._bifrost_config_changed)

    def test_a_failed_copy_fails_and_marks_nothing(self):
        run, report, fail = self.place("linux", run_rc=1)
        fail.assert_called_once()
        self.assertFalse(bootstrap._bifrost_config_changed)

    def test_elsewhere_the_file_is_written_directly(self):
        run, report, fail = self.place("darwin")
        run.assert_not_called(); fail.assert_not_called()
        self.assertEqual(self.target.read_bytes(), CONFIG)
        self.assertTrue(bootstrap._bifrost_config_changed)

    def test_elsewhere_a_write_error_is_reported_not_raised(self):
        with mock.patch.object(bootstrap, "write_atomic", side_effect=PermissionError()):
            run, report, fail = self.place("darwin")
        fail.assert_called_once()
        self.assertFalse(bootstrap._bifrost_config_changed)

    def test_a_missing_checkout_copy_fails_cleanly(self):
        (self.root / "bifrost" / "config.json").unlink()
        run, report, fail = self.place("linux")
        fail.assert_called_once()
        run.assert_not_called()


class RestartBifrost(unittest.TestCase):
    def test_restart_uses_compose_and_reports_failure(self):
        for rc, expected in ((0, True), (1, False)):
            with self.subTest(rc=rc), mock.patch.object(bootstrap, "compose",
                                                        return_value=subprocess.CompletedProcess([], rc, "", "")) as compose, \
                    mock.patch.object(bootstrap, "report"), mock.patch.object(bootstrap, "fail") as fail, \
                    mock.patch.object(bootstrap, "_failed", False):
                self.assertIs(bootstrap._restart_bifrost(), expected)
                self.assertEqual(compose.call_args.args, ("restart", "bifrost"))
                self.assertEqual(fail.called, not expected)


if __name__ == "__main__":
    unittest.main()
