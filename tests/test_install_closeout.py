"""B06-1 install.py: the end of a run after the set is published (reviewer A finding 9, reviewer B P1).

A reader that went away (a closed pipe) must not turn a published set into exit 120 and a traceback: the exit status stays
the result's, which is 0 here, and the set is on disk. The failure used to surface only when the interpreter flushed stdout
at exit, so it needs a real process.

Run: python tests/test_install_closeout.py   (Linux; synthetic data and temporary directories only; skipped elsewhere)
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import install_fixtures as fx  # noqa: E402
from install_fixtures import install  # noqa: E402

SCRIPT = Path(install.__file__)
linux = unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux (renameat2, modes, pipes)")


@linux
class ClosedPipeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.repo, self.sha = fx.make_repo(self.tmp / "repo")
        self.parent = self.tmp / "cfg"
        self.parent.mkdir(mode=0o700)
        os.chmod(self.parent, 0o700)
        self.target = self.parent / "set"

    def run_with_dead_reader(self, command="init-config"):
        read_fd, write_fd = os.pipe()
        os.close(read_fd)                                    # nobody reads: every write to stdout fails with EPIPE
        env = {k: v for k, v in os.environ.items() if k not in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_URL")}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        proc = subprocess.Popen([sys.executable, str(SCRIPT), command, "--source-sha", self.sha, "--target-dir", str(self.target),
                                 "--repo", str(self.repo)], stdin=subprocess.DEVNULL, stdout=write_fd, stderr=subprocess.PIPE, env=env)
        os.close(write_fd)
        _, err = proc.communicate(timeout=60)
        return proc.returncode, err

    def test_a_published_set_keeps_exit_0_when_nobody_reads_stdout(self):
        code, err = self.run_with_dead_reader()
        self.assertEqual(code, 0, err.decode("utf-8", "replace")[-300:])
        self.assertEqual(sorted(os.listdir(self.target)), [".env", "install-record.json"])
        self.assertNotIn(b"Traceback", err)
        self.assertNotIn(b"Exception ignored", err)

    def test_a_read_only_command_keeps_its_exit_status_too(self):
        code, err = self.run_with_dead_reader("plan")
        self.assertEqual(code, 0, err.decode("utf-8", "replace")[-300:])
        self.assertFalse(self.target.exists())
        self.assertNotIn(b"Exception ignored", err)

    def test_a_repeated_run_keeps_exit_2_when_nobody_reads_stdout(self):
        self.assertEqual(self.run_with_dead_reader()[0], 0)
        code, err = self.run_with_dead_reader()
        self.assertEqual(code, 2)                            # refused: the existing set, never regenerated
        self.assertNotIn(b"Exception ignored", err)


if __name__ == "__main__":
    unittest.main()
