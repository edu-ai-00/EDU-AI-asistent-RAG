"""Independent review A of B06-1 (R1 publication, R4 signals): reproductions that are kept as regression tests.

Run: python tests/test_install_cleanup_safety.py   (Linux; standard library + git; synthetic data and temporary directories only)

Windows or any non-Linux host: every test is skipped. Races are made deterministic with an audit hook that runs in the process
immediately BEFORE the audited operation (so the real code path is exercised, not a mock of it), or with a real child process that
freezes itself (SIGSTOP) at that instant while this process acts, then is resumed or signalled from outside.
"""
import errno
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import install_fixtures as fx  # noqa: E402
from install_fixtures import install  # noqa: E402

linux = unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux (renameat2, signals, modes)")
TESTS = str(Path(__file__).resolve().parent)
FILES = [".env", "install-record.json"]

# one permanent hook (audit hooks cannot be removed); a test arms it with a function (event, args) -> None
_ARMED = [None]


def _dispatch(event, args):
    fn = _ARMED[0]
    if fn is not None:
        fn(event, args)


sys.addaudithook(_dispatch)

FREEZE_CHILD = r'''
import os, signal, sys
from pathlib import Path
sys.path.insert(0, os.environ["TESTS"])
import install_fixtures as fx
install = fx.install
signal.signal(signal.SIGINT, signal.default_int_handler)
event, match, mark, force = os.environ["EVENT"], os.environ.get("MATCH", ""), Path(os.environ["MARK"]), os.environ.get("FORCE_EEXIST")
armed, done = [False], []

def hook(name, args):
    if armed[0] and name == event and not done and match in str(args[0] if args else ""):
        done.append(1)
        mark.write_text(str(args[0]))
        os.kill(os.getpid(), signal.SIGSTOP)

def fault(step):
    if step == "before_publish" and force:
        os.mkdir(sys.argv[2], 0o755)
    if step == "begin" or (step == "before_publish" and not force):
        armed[0] = True

sys.addaudithook(hook)
report = install.init_config(sys.argv[1], sys.argv[2], sys.argv[3], gen=fx.STUB_GEN, fault=fault)
report.emit()
sys.exit(report.exit_code())
'''


def tree(path):
    path = Path(path)
    info = os.lstat(path)
    entry = {"ino": info.st_ino, "mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "mtime": info.st_mtime_ns}
    if stat.S_ISLNK(info.st_mode):
        entry["link"] = os.readlink(path)
    elif stat.S_ISREG(info.st_mode):
        entry["data"] = path.read_bytes()
    elif stat.S_ISDIR(info.st_mode):
        entry["children"] = {n: tree(path / n) for n in sorted(os.listdir(path))}
    return entry


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.repo, self.sha = fx.make_repo(self.tmp / "repo")
        self.addCleanup(self.disarm)
        self.fresh_parent()

    def disarm(self):
        _ARMED[0] = None

    def fresh_parent(self):
        self.parent = Path(tempfile.mkdtemp(dir=self.tmp, prefix="cfg_"))
        os.chmod(self.parent, 0o700)
        self.target = self.parent / "set"

    def init(self, **kw):
        kw.setdefault("gen", fx.STUB_GEN)
        try:
            return install.init_config(self.sha, str(self.target), str(self.repo), **kw)
        except KeyboardInterrupt:
            self.fail("an interrupt escaped init_config")

    def staging(self):
        names = [n for n in os.listdir(self.parent) if ".staging-" in n]
        return [self.parent / n for n in names]

    def assertNoSecrets(self, report):
        text = "\n".join(report.lines)
        for value in fx.STUB_VALUES.values():
            self.assertNotIn(value, text)

    def foreign_empty_dir(self, path, mode=0o750):
        os.mkdir(path, mode)
        os.chmod(path, mode)
        os.utime(path, (1_000_000_000, 1_000_000_000))
        return tree(path)


@linux
class CleanupNeverRemovesAForeignDirectoryTests(Base):
    """R1: at EVERY audit event of the cleanup, an empty foreign directory that takes the staging name survives (the old rmdir by name deleted it)."""

    def run_with_swap_at(self, nth):
        """A run that fails (a concurrent winner made the target) and, at the nth audit event after the failed rename, swaps in a foreign directory."""
        self.fresh_parent()
        state = {"after": False, "n": 0, "busy": False}

        def hook(event, args):
            if event == "ctypes.call_function":
                state["after"] = True
                return
            if not state["after"] or state["busy"] or "foreign" in state or event.startswith(("sys.", "import", "compile", "exec", "marshal")):
                return
            state["n"] += 1
            if state["n"] == nth:
                state["busy"] = True
                try:
                    names = [n for n in os.listdir(self.parent) if ".staging-" in n]
                    if len(names) != 1:                          # already removed: nothing to swap at this instant
                        return
                    name = names[0]
                    os.rename(self.parent / name, self.parent / "moved-staging")
                    state["foreign"] = (self.parent / name, self.foreign_empty_dir(self.parent / name))
                finally:
                    state["busy"] = False

        def fault(step):
            if step == "before_publish":
                os.mkdir(self.target, 0o755)

        _ARMED[0] = hook
        try:
            report = self.init(fault=fault)
        finally:
            _ARMED[0] = None
        return report, state

    def test_an_empty_foreign_directory_swapped_in_at_any_instant_of_the_cleanup_survives(self):
        _, probe = self.run_with_swap_at(10 ** 6)               # how many audit events does the cleanup have?
        total = probe["n"]
        self.assertGreater(total, 3)
        swapped = 0
        for nth in range(1, total + 1):
            report, state = self.run_with_swap_at(nth)
            if "foreign" not in state:
                continue
            swapped += 1
            path, before = state["foreign"]
            with self.subTest(event_number=nth):
                self.assertTrue(os.path.lexists(path), f"the foreign empty directory was deleted by the cleanup; {report.lines}")
                after = tree(path)
                self.assertEqual((after["ino"], after["mode"], after["mtime"], after["children"]),
                                 (before["ino"], before["mode"], before["mtime"], {}))
                self.assertNoSecrets(report)
        self.assertGreater(swapped, 3, "the swap seam was not reached")

    def test_a_real_second_process_swaps_an_empty_directory_while_the_child_is_frozen_in_the_cleanup(self):
        """A frozen child (SIGSTOP at the audit event), a swap by this other process, SIGCONT. os.truncate is the child's first step of the
        cleanup (our own file is emptied through its descriptor); os.remove and os.rmdir were the by-name steps the old code could be frozen at."""
        for event, must_be_reached in (("os.truncate", True), ("os.remove", False), ("os.rmdir", False)):
            self.fresh_parent()
            mark = self.tmp / f"mark_{event}"
            child = subprocess.Popen([sys.executable, "-c", FREEZE_CHILD, self.sha, str(self.target), str(self.repo)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     env={**os.environ, "TESTS": TESTS, "EVENT": event, "MATCH": "", "MARK": str(mark), "FORCE_EEXIST": "1"})
            deadline = time.time() + 60
            while not mark.exists() and child.poll() is None:
                self.assertLess(time.time(), deadline)
                time.sleep(0.005)
            with self.subTest(event=event):
                if not mark.exists():
                    self.assertFalse(must_be_reached, f"the cleanup never reached {event}: {child.communicate()}")
                    child.communicate()
                    continue
                while Path(f"/proc/{child.pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "T":
                    time.sleep(0.005)
                (staging,) = self.staging()
                os.rename(staging, self.parent / "moved-staging")
                before = self.foreign_empty_dir(staging)
                os.kill(child.pid, signal.SIGCONT)
                out, err = child.communicate(timeout=60)
                self.assertTrue(os.path.lexists(staging), "the foreign empty directory was deleted by the child's cleanup")
                self.assertEqual((tree(staging)["ino"], tree(staging)["mode"]), (before["ino"], before["mode"]))
                moved = self.parent / "moved-staging"
                self.assertEqual(sorted(os.listdir(moved)), FILES)
                self.assertEqual([(moved / n).stat().st_size for n in FILES], [0, 0], "our own files must be emptied in the directory we created")


@linux
class StagingAdoptionTests(Base):
    """R1: the staging NAME is opened after mkdtemp, so a directory swapped in between is not identifiable by inode. Whatever it is, it must
    not be chmod-ed, filled with secrets, published, or have files removed from it (a foreign .env / install-record.json especially)."""

    def swap_before_open(self, make):
        state = {"busy": False}

        def hook(event, args):
            if event == "open" and not state.get("done") and not state["busy"] and ".staging-" in str(args[0]) and ((args[2] or 0) & os.O_DIRECTORY):
                state["busy"] = True
                try:
                    state["done"] = True
                    path = Path(args[0])
                    os.rename(path, self.parent / "moved-staging")
                    state["path"], state["before"] = path, make(path)
                finally:
                    state["busy"] = False

        _ARMED[0] = hook
        return state

    def assertForeignUntouched(self, state):
        path, before = state["path"], state["before"]
        self.assertTrue(os.path.lexists(path), "the foreign directory (or its files) was deleted")
        after = tree(path)
        self.assertEqual((after["ino"], after["mode"], after["mtime"]), (before["ino"], before["mode"], before["mtime"]))
        self.assertEqual(after["children"], before["children"])

    def test_a_foreign_directory_that_is_not_an_empty_0700_one_is_refused_untouched(self):
        def empty_0755(p): return self.foreign_empty_dir(p, 0o755)

        def with_env_and_record(p):
            os.mkdir(p, 0o700)
            (p / ".env").write_text("FOREIGN_SECRET=do-not-delete\n")
            (p / "install-record.json").write_text('{"foreign": true}\n')
            os.utime(p, (1_000_000_000, 1_000_000_000))
            return tree(p)

        def with_env_only(p):
            os.mkdir(p, 0o700)
            (p / ".env").write_text("FOREIGN_SECRET=do-not-delete\n")
            os.utime(p, (1_000_000_000, 1_000_000_000))
            return tree(p)

        def with_content(p):
            os.mkdir(p, 0o700)
            (p / "keep.txt").write_text("keep")
            os.utime(p, (1_000_000_000, 1_000_000_000))
            return tree(p)

        for make in (empty_0755, with_env_and_record, with_env_only, with_content):
            self.fresh_parent()
            state = self.swap_before_open(make)
            try:
                report = self.init()
            finally:
                self.disarm()
            with self.subTest(kind=make.__name__):
                self.assertTrue(state.get("done"), "the seam was not reached")
                self.assertEqual(report.exit_code(), 3, report.lines)
                self.assertNotIn("CONFIG_PREPARED", report.lines)
                self.assertFalse(self.target.exists(), "a foreign directory was published as the configuration set")
                self.assertForeignUntouched(state)
                self.assertNoSecrets(report)

    def test_an_empty_0700_directory_that_cannot_be_told_apart_is_never_deleted_when_the_run_fails(self):
        """The one case identity cannot exclude (same user, empty, 0700, random name): it may be used, but a failure must not delete it."""
        def fault(step):
            if step == "before_publish":
                os.mkdir(self.target, 0o755)

        state = self.swap_before_open(lambda p: self.foreign_empty_dir(p, 0o700))
        try:
            report = self.init(fault=fault)
        finally:
            self.disarm()
        self.assertTrue(state.get("done"))
        self.assertEqual(report.exit_code(), 2, report.lines)
        self.assertTrue(os.path.lexists(state["path"]), "the adopted empty directory was deleted by the cleanup")
        self.assertEqual(sorted(os.listdir(state["path"])), FILES)                    # the files we wrote into it stay, at zero length
        self.assertEqual([(state["path"] / n).stat().st_size for n in FILES], [0, 0])
        self.assertTrue(any(l.startswith("STAGING_LEFT_EMPTIED: " + str(state["path"])) for l in report.lines), report.lines)


@linux
class SmallerFindingsTests(Base):
    def test_a_fifo_named_like_the_record_does_not_hang_the_preflight(self):
        os.mkdir(self.target, 0o700)
        (self.target / ".env").write_text("LANGFLOW_SECRET_KEY=x\n")
        os.mkfifo(self.target / "install-record.json")
        cmd = [sys.executable, str(Path(install.__file__)), "preflight", "--source-sha", self.sha, "--target-dir", str(self.target), "--repo", str(self.repo)]
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired:
            self.fail("preflight hangs on a FIFO named install-record.json (blocking open of a foreign object)")
        self.assertIn(done.returncode, (2, 3), done.stdout)

    def hold_before_rename(self, signum):
        code = r'''
import os, signal, sys, time
from pathlib import Path
sys.path.insert(0, os.environ["TESTS"])
import install_fixtures as fx
install = fx.install
signal.signal(signal.SIGINT, signal.default_int_handler)
real = install._rename_noreplace
def rename(src, dst, *rest):
    Path(os.environ["READY"]).write_text("held")
    end = time.time() + 30
    while time.time() < end:
        time.sleep(0.02)
    real(src, dst, *rest)
install._rename_noreplace = rename
report = install.init_config(sys.argv[1], sys.argv[2], sys.argv[3], gen=fx.STUB_GEN)
report.emit()
sys.exit(report.exit_code())
'''
        ready = self.tmp / "ready"
        child = subprocess.Popen([sys.executable, "-c", code, self.sha, str(self.target), str(self.repo)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, env={**os.environ, "TESTS": TESTS, "READY": str(ready)})
        deadline = time.time() + 60
        while not ready.exists():
            self.assertIsNone(child.poll())
            self.assertLess(time.time(), deadline)
            time.sleep(0.01)
        os.kill(child.pid, signum)
        out, err = child.communicate(timeout=60)
        return child.returncode, out, err

    def test_sighup_the_signal_of_a_closed_terminal_cleans_up_like_sigterm(self):
        code, out, err = self.hold_before_rename(signal.SIGHUP)
        self.assertEqual(code, 3, (out, err))
        self.assertIn("UNVERIFIED INTERRUPTED", out)
        self.assertFalse(self.target.exists())
        self.assertEqual(fx.secret_staging(self.parent), [], "plaintext secrets were left behind by a SIGHUP")
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
