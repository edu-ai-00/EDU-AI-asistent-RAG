"""B06-1-R1 (foreign files and directories) and B06-1-R4 (interrupt while publishing): regression tests for install.py init-config.

Run: python tests/test_install_publication_regression.py   (Linux; standard library + git; synthetic data and temporary directories only)
Everything here needs renameat2, POSIX modes and real signals, so on other systems every test is skipped (reported as skipped, not passed).

Conventions: a "foreign" object is made by the test itself, not by init-config. Its identity (device, inode), mode, owner, mtime and
content are recorded when it appears and must be identical after the run. Races are made deterministic with the `fault` seam
(named steps), by wrapping the one function that touches the disk at the interesting instant, or by a real child process that is
held at that instant and then sent a real SIGINT/SIGTERM from outside.
"""
import ast
import contextlib
import errno
import hashlib
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import install_fixtures as fx  # noqa: E402
from install_fixtures import install  # noqa: E402

linux = unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux (renameat2, signals, modes)")
FILES = [".env", "install-record.json"]
TESTS = str(Path(__file__).resolve().parent)

# A real child process that is held (sleeping in Python code) at one instant of init-config and then interrupted by the test.
CHILD_CODE = r'''
import errno, os, signal, sys, time
from pathlib import Path
sys.path.insert(0, os.environ["TESTS"])
import install_fixtures as fx
install = fx.install
signal.signal(signal.SIGINT, signal.default_int_handler)    # a runner started in the background may have SIGINT ignored
window, ready = os.environ["WINDOW"], Path(os.environ["READY"])


def hold():
    ready.write_text("held")
    end = time.time() + 30
    while time.time() < end:
        time.sleep(0.02)                                     # the signal handler raises KeyboardInterrupt in here


real_rename, real_fsync, real_discard = install._rename_noreplace, install._fsync_dir, install._discard
if window == "before_syscall":
    def rename(src, dst, *rest):
        hold()
        real_rename(src, dst, *rest)
    install._rename_noreplace = rename
elif window == "after_syscall":
    def rename(src, dst, *rest):
        real_rename(src, dst, *rest)
        hold()
    install._rename_noreplace = rename
elif window == "parent_fsync":
    calls = []
    def fsync_dir(*args, **kw):
        real_fsync(*args, **kw)
        calls.append(1)
        if len(calls) == 2:
            hold()
    install._fsync_dir = fsync_dir
elif window == "during_cleanup":
    def rename(src, dst, *rest):
        raise OSError(errno.EIO, "injected")
    def discard(*args, **kw):
        hold()
        real_discard(*args, **kw)
    install._rename_noreplace, install._discard = rename, discard
report = install.init_config(sys.argv[1], sys.argv[2], sys.argv[3], gen=fx.STUB_GEN)
report.emit()
sys.exit(report.exit_code())
'''


def tree(path):
    """Everything observable about an object and what is inside it; links are not followed. Includes mtime, excludes atime and ctime."""
    path = Path(path)
    info = os.lstat(path)
    entry = {"dev": info.st_dev, "ino": info.st_ino, "mode": info.st_mode, "uid": info.st_uid, "mtime": info.st_mtime_ns}
    if stat.S_ISLNK(info.st_mode):
        entry["link"] = os.readlink(path)
    elif stat.S_ISREG(info.st_mode):
        entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    elif stat.S_ISDIR(info.st_mode):
        entry["children"] = {name: tree(path / name) for name in sorted(os.listdir(path))}
    return entry


# kinds of foreign object a second process might leave at a path
def make_empty_dir(p):
    os.mkdir(p, 0o755)


def make_dir_with_content(p):
    os.mkdir(p, 0o755)
    (p / "keep.txt").write_text("keep")
    os.mkdir(p / "sub", 0o750)
    (p / "sub" / "deep.txt").write_text("deep")


def make_file(p):
    p.write_text("a foreign file")


def make_symlink_to_dir(p):
    real = p.parent / ("linked_" + p.name)
    real.mkdir()
    (real / "sentinel").write_text("sentinel")
    os.symlink(real, p)


def make_dangling_symlink(p):
    os.symlink(p.parent / "missing", p)


def make_fifo(p):
    os.mkfifo(p, 0o600)


def make_lookalike_config_set(p):
    fx.write_config_set(p, "k" * 43 + "=")


FOREIGN = {f.__name__[5:]: f for f in (make_empty_dir, make_dir_with_content, make_file, make_symlink_to_dir,
                                       make_dangling_symlink, make_fifo, make_lookalike_config_set)}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.repo, self.sha = fx.make_repo(self.tmp / "repo")
        self.fresh_parent()

    def fresh_parent(self):
        self.parent = Path(tempfile.mkdtemp(dir=self.tmp, prefix="cfg_"))
        os.chmod(self.parent, 0o700)
        self.target = self.parent / "set"

    def init(self, **kw):
        kw.setdefault("gen", fx.STUB_GEN)
        try:
            return install.init_config(self.sha, str(self.target), str(self.repo), **kw)
        except KeyboardInterrupt:       # unittest would abort the whole run; a leaked interrupt is a failure of this test only
            self.fail("an interrupt escaped init_config: the run ended without a report")

    def lines(self, report):
        return "\n".join(report.lines)

    def assertNoSecrets(self, report):
        for value in fx.STUB_VALUES.values():
            self.assertNotIn(value, self.lines(report))

    def staging_names(self):
        """Staging directories that still hold something; an emptied one is the announced leftover of the cleanup (never removed by name)."""
        return fx.secret_staging(self.parent)

    def staging_path(self):
        (name,) = [n for n in os.listdir(self.parent) if ".staging-" in n]          # any staging directory, emptied or not
        return self.parent / name

    def foreign_state(self, staging=False):
        """Every entry of the parent; our own staging directory is left out unless `staging` (then a foreign object at that name counts)."""
        return {n: tree(self.parent / n) for n in sorted(os.listdir(self.parent)) if staging or ".staging-" not in n}

    def swap_staging_for(self, make):
        """Move our staging directory away (it keeps its inode and files) and put a foreign object at its name."""
        staging = self.staging_path()
        moved = self.parent / "moved-staging"
        os.rename(staging, moved)
        make(staging)
        return staging, moved

    def assertEmptied(self, entry, names=FILES):
        """A tree() entry of a directory this run created: exactly the files it had written, all at zero length (no secret is left)."""
        self.assertEqual(sorted(entry["children"]), sorted(names))
        for name, child in entry["children"].items():
            self.assertEqual(child["sha256"], hashlib.sha256(b"").hexdigest(), name)

    def assertAnnounced(self, report, staging, *, others=0, token="STAGING_LEFT_EMPTIED"):
        """The report names the full path of the staging directory and says what was emptied and what else is in it."""
        (line,) = [l for l in report.lines if l.startswith(token + ": ")]
        self.assertTrue(os.path.isabs(str(staging)))
        self.assertTrue(line.startswith(f"{token}: {staging} ("), line)
        self.assertIn(f"{others} other " + ("entry is" if others == 1 else "entries are") + " present and left untouched", line)
        return line

    def assertPublishedAndComplete(self, target=None):
        target = Path(target or self.target)
        self.assertEqual(sorted(os.listdir(target)), sorted(FILES))
        self.assertEqual(oct(os.stat(target).st_mode & 0o7777), "0o700")
        self.assertEqual(os.stat(target).st_uid, os.geteuid())
        for name in FILES:
            self.assertEqual(oct(os.stat(target / name).st_mode & 0o7777), "0o600")
        self.assertEqual(install.key_fingerprint_state(str(target)), "MATCH")


@linux
class R1ForeignObjectAtTheTargetTests(Base):
    """An object created or put in place by another process at the target is never overwritten, replaced, opened, moved or removed."""

    STEPS = ("begin", "staged_dir", "env_written", "record_written", "before_publish")

    def test_a_foreign_object_appearing_at_any_step_is_refused_and_left_exactly_as_it_was(self):
        for step in self.STEPS:
            for kind, make in FOREIGN.items():
                self.fresh_parent()
                seen = {}

                def fault(s, make=make, step=step):
                    if s == step:
                        make(self.target)
                        seen.update(self.foreign_state())

                report = self.init(fault=fault)
                with self.subTest(step=step, kind=kind):
                    self.assertEqual(report.exit_code(), 2, report.lines)
                    self.assertTrue(report.refused[0].startswith("TARGET_EXISTS_"), report.refused)
                    self.assertNotIn("CONFIG_PREPARED", report.lines)
                    self.assertEqual(self.foreign_state(), seen)               # identity, mode, owner, mtime, content
                    self.assertEqual(self.staging_names(), [])                  # our own staging holds no secret any more
                    self.assertNoSecrets(report)

    def test_a_foreign_object_replaced_by_another_one_at_a_later_step_stays_untouched(self):
        seen = {}

        def fault(step):
            if step == "staged_dir":
                make_dir_with_content(self.target)
            elif step == "before_publish":
                os.rename(self.target, self.parent / "first-foreign")
                make_empty_dir(self.target)
                seen.update(self.foreign_state())

        report = self.init(fault=fault)
        self.assertEqual(report.exit_code(), 2, report.lines)
        self.assertEqual(self.foreign_state(), seen)
        self.assertEqual(os.listdir(self.target), [])
        self.assertEqual(sorted(os.listdir(self.parent / "first-foreign")), ["keep.txt", "sub"])
        self.assertEqual(self.staging_names(), [])

    def test_a_foreign_object_made_by_a_real_second_process_during_the_pause_is_left_alone(self):
        child = subprocess.Popen([sys.executable, str(Path(TESTS) / "install_child.py"), self.sha, str(self.target), str(self.repo)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 env={**os.environ, "FAULT_STEP": "before_publish", "FAULT_MODE": "pause",
                                      "PAUSE_FILE": str(self.tmp / "paused"), "GO_FILE": str(self.tmp / "go")})
        deadline = time.time() + 30
        while not (self.tmp / "paused").exists():
            self.assertLess(time.time(), deadline, "the child never reached the publication")
            time.sleep(0.01)
        make_dir_with_content(self.target)
        before = self.foreign_state()
        (self.tmp / "go").write_text("go")
        out, err = child.communicate(timeout=60)
        self.assertEqual(child.returncode, 2, (out, err))
        self.assertIn("REFUSED TARGET_EXISTS_FOREIGN_DIRECTORY", out)
        self.assertEqual(self.foreign_state(), before)
        self.assertEqual(self.staging_names(), [])

    def test_the_cleanup_never_touches_the_target_path_even_when_it_holds_files_with_our_names(self):
        seen = {}
        real = os.ftruncate
        done = []

        def ftruncate(fd, *a, **kw):
            real(fd, *a, **kw)
            if not done:
                done.append(1)
                os.mkdir(self.target, 0o755)
                (self.target / ".env").write_text("foreign env")
                (self.target / "install-record.json").write_text("foreign record")
                seen.update(self.foreign_state())

        def fault(step):
            if step == "record_written":
                raise OSError(errno.EIO, "injected")

        with mock.patch.object(install.os, "ftruncate", ftruncate):
            report = self.init(fault=fault)
        self.assertEqual(report.exit_code(), 3, report.lines)
        self.assertTrue(seen, "the cleanup did not run")
        self.assertEqual(self.foreign_state(), seen)
        self.assertEqual(self.staging_names(), [])


@linux
class R1StagingIdentityTests(Base):
    """Our secrets go only into the directory this run created, and only that directory is published or cleaned up."""

    def swap_kinds(self):
        def empty(p): os.mkdir(p, 0o755)
        def with_content(p): make_dir_with_content(p)
        def symlink_to_foreign_dir(p):
            foreign = self.parent / "elsewhere"
            foreign.mkdir(mode=0o755)
            (foreign / "keep.txt").write_text("keep")
            os.symlink(foreign, p)
        def regular_file(p): p.write_text("a foreign file")
        return {"empty_dir": empty, "dir_with_content": with_content, "symlink_to_dir": symlink_to_foreign_dir, "file": regular_file}

    def test_a_staging_name_that_is_replaced_after_creation_receives_no_secret_and_is_not_published(self):
        for kind in self.swap_kinds():
            self.fresh_parent()
            seen = {}

            def fault(step, kind=kind):
                if step == "staged_dir":
                    self.swap_staging_for(self.swap_kinds()[kind])
                    seen.update(self.foreign_state(staging=True))

            report = self.init(fault=fault)
            with self.subTest(kind=kind):
                self.assertEqual(report.exit_code(), 3, report.lines)
                self.assertNotIn("CONFIG_PREPARED", report.lines)
                self.assertFalse(self.target.exists())
                after = self.foreign_state(staging=True)
                after_moved = after.pop("moved-staging")
                seen_moved = seen.pop("moved-staging")
                self.assertEqual(after, seen)                                   # the foreign object and everything beside it: untouched
                self.assertEqual(after_moved["ino"], seen_moved["ino"])
                self.assertEmptied(after_moved)                                 # our files are emptied in the directory we created
                self.assertNoSecrets(report)

    def test_a_staging_name_replaced_between_the_self_check_and_the_publication_is_not_published(self):
        seen = {}

        def fault(step):
            if step == "before_publish":
                self.swap_staging_for(make_dir_with_content)
                seen.update(self.foreign_state(staging=True))

        report = self.init(fault=fault)
        self.assertEqual(report.exit_code(), 3, report.lines)
        self.assertEqual(report.unverified, ["STAGING_REPLACED"])
        self.assertNotIn("CONFIG_PREPARED", report.lines)
        self.assertFalse(self.target.exists(), "a foreign directory was published as the configuration set")
        after, seen_moved = self.foreign_state(staging=True), seen.pop("moved-staging")
        self.assertEqual(after.pop("moved-staging")["ino"], seen_moved["ino"])
        self.assertEqual(after, seen)
        self.assertNoSecrets(report)

    def test_a_staging_name_replaced_inside_the_publishing_call_is_reported_and_nothing_is_removed(self):
        real, state = install._rename_noreplace, {}

        def rename(src, dst, *rest):
            state["moved"] = self.swap_staging_for(make_dir_with_content)[1]     # lost the race: the foreign directory is renamed
            state["foreign"] = tree(self.parent / os.path.basename(src))
            real(src, dst, *rest)

        with mock.patch.object(install, "_rename_noreplace", rename):
            report = self.init()
        self.assertEqual(report.exit_code(), 3, report.lines)
        self.assertEqual(report.unverified, ["PUBLICATION_STATE_UNKNOWN"])
        self.assertNotIn("CONFIG_PREPARED", report.lines)
        self.assertFalse(any("nothing published" in l for l in report.lines), report.lines)
        self.assertEqual(sorted(os.listdir(self.target)), ["keep.txt", "sub"])
        self.assertEqual(tree(self.target)["ino"], state["foreign"]["ino"])
        self.assertEqual(tree(self.target)["children"], state["foreign"]["children"])
        self.assertEqual(sorted(os.listdir(state["moved"])), sorted(FILES))      # unclear state: our files are not removed
        self.assertIn(os.path.realpath(state["moved"]), self.lines(report))      # and the operator is told where they are
        self.assertNoSecrets(report)

    def test_a_target_swapped_after_the_publication_is_reported_not_claimed(self):
        def fault(step):
            if step == "renamed":
                os.rename(self.target, self.parent / "ours-moved")
                make_dir_with_content(self.target)

        report = self.init(fault=fault)
        self.assertEqual(report.exit_code(), 3, report.lines)
        self.assertEqual(report.unverified, ["PUBLICATION_STATE_UNKNOWN"])
        self.assertNotIn("CONFIG_PREPARED", report.lines)
        self.assertEqual(sorted(os.listdir(self.target)), ["keep.txt", "sub"])
        self.assertEqual(sorted(os.listdir(self.parent / "ours-moved")), sorted(FILES))
        self.assertIn(os.path.realpath(self.parent / "ours-moved"), self.lines(report))
        self.assertNoSecrets(report)

    def test_a_lookalike_set_swapped_in_after_the_publication_is_not_taken_for_ours(self):
        def fault(step):
            if step == "renamed":
                os.rename(self.target, self.parent / "ours-moved")
                make_lookalike_config_set(self.target)            # a consistent set with a different key

        report = self.init(fault=fault)
        self.assertEqual(report.unverified, ["PUBLICATION_STATE_UNKNOWN"], report.lines)
        self.assertNotIn("CONFIG_PREPARED", report.lines)

    def test_a_parent_directory_swapped_after_the_staging_was_created_gets_nothing_and_loses_nothing(self):
        """The parent path is resolved once; a swap of the whole directory can only make the run fail, never publish or remove anything."""
        old_parent = self.parent.with_name(self.parent.name + "-old")

        def fault(step):
            if step == "staged_dir":
                os.rename(self.parent, old_parent)
                os.mkdir(self.parent, 0o700)                 # same path, a different directory
                (self.parent / "bystander.txt").write_text("keep")
                self.seen = self.foreign_state()

        report = self.init(fault=fault)
        self.assertEqual(report.exit_code(), 3, report.lines)
        self.assertEqual(report.unverified, ["STAGING_REPLACED"], report.lines)
        self.assertNotIn("CONFIG_PREPARED", report.lines)
        self.assertEqual(self.foreign_state(staging=True), self.seen)        # the new directory: nothing created, nothing removed
        (stale,) = [n for n in os.listdir(old_parent) if ".staging-" in n]
        self.assertEmptied(tree(old_parent / stale))                          # our files are emptied in the directory we created
        self.assertIn(str(old_parent / stale), self.assertAnnounced(report, self.parent / stale))      # and the operator is told where it is now
        self.assertNoSecrets(report)

    def test_a_staging_name_that_is_a_link_right_after_mkdtemp_is_not_followed_or_changed(self):
        foreign = self.parent / "elsewhere"
        foreign.mkdir(mode=0o755)
        (foreign / "keep.txt").write_text("keep")
        before = tree(foreign)
        real = tempfile.mkdtemp

        def mkdtemp(*args, **kw):
            made = real(*args, **kw)
            os.rmdir(made)
            os.symlink(foreign, made)                            # the name was swapped for a link right after it was made
            return made

        with mock.patch.object(install.tempfile, "mkdtemp", mkdtemp):
            report = self.init()
        self.assertEqual(report.exit_code(), 3, report.lines)
        self.assertNotIn("CONFIG_PREPARED", report.lines)
        self.assertEqual(tree(foreign), before)                  # no chmod, no file written through the link
        self.assertEqual(oct(os.lstat(foreign).st_mode & 0o7777), "0o755")
        self.assertFalse(self.target.exists())
        self.assertNoSecrets(report)

    def test_the_mode_of_the_staging_directory_is_set_through_the_descriptor_not_the_name(self):
        foreign = self.parent / "elsewhere"
        foreign.mkdir(mode=0o755)
        before = tree(foreign)
        real, done = os.fstat, []

        def fstat(fd, *a, **kw):
            result = real(fd, *a, **kw)
            if not done and isinstance(fd, int):             # the first descriptor looked at is the staging directory just opened
                done.append(1)
                staging = self.staging_path()
                os.rename(staging, self.parent / "moved-staging")
                os.symlink(foreign, staging)                 # from now on the name leads to a directory that is not ours
            return result

        with mock.patch.object(install.os, "fstat", fstat):
            report = self.init()
        self.assertTrue(done, "the seam was not reached")
        self.assertEqual(tree(foreign), before)              # a chmod by name would have made it 0700
        self.assertNotIn("CONFIG_PREPARED", report.lines)
        self.assertFalse(self.target.exists())
        self.assertNoSecrets(report)

    def test_a_staged_set_with_a_loosened_mode_or_an_intruder_is_never_published(self):
        def loosen_dir(path): os.chmod(path, 0o755)
        def loosen_env(path): os.chmod(path / ".env", 0o644)
        def loosen_record(path): os.chmod(path / "install-record.json", 0o640)
        def intruder(path): (path / "intruder.txt").write_text("not ours")
        for change in (loosen_dir, loosen_env, loosen_record, intruder):
            self.fresh_parent()

            def fault(step, change=change):
                if step == "record_written":
                    change(self.staging_path())

            report = self.init(fault=fault)
            with self.subTest(change=change.__name__):
                self.assertEqual(report.exit_code(), 3, report.lines)
                self.assertEqual(report.unverified, ["STAGED_SET_FAILED_SELF_CHECK"], report.lines)
                self.assertNotIn("CONFIG_PREPARED", report.lines)
                self.assertFalse(self.target.exists())
                self.assertAnnounced(report, self.staging_path(), others=int(change is intruder))
                if change is intruder:                       # not ours, so neither removed nor followed
                    self.assertEqual((self.staging_path() / "intruder.txt").read_text(), "not ours")
                    self.assertEqual(self.staging_names(), [self.staging_path().name])     # a directory with a foreign entry is not "emptied"
                else:
                    self.assertEqual(self.staging_names(), [])
                self.assertNoSecrets(report)

    def test_a_foreign_link_planted_inside_the_staging_directory_is_not_written_through(self):
        victim = self.parent / "victim.txt"
        victim.write_text("precious")
        before = tree(victim)

        def fault(step):
            if step == "staged_dir":
                os.symlink(victim, self.staging_path() / ".env")

        report = self.init(fault=fault)
        self.assertEqual(report.exit_code(), 3, report.lines)
        self.assertEqual(tree(victim), before)
        line = self.assertAnnounced(report, self.staging_path(), others=1)
        self.assertIn("0 of 0 files", line)                                  # the planted link kept this run from creating anything
        self.assertTrue(os.path.islink(self.staging_path() / ".env"))        # not ours, so not removed
        self.assertNoSecrets(report)

    def test_a_staging_directory_that_is_not_owned_by_this_user_is_not_used_or_removed(self):
        real = os.geteuid
        calls = []

        def geteuid():
            calls.append(1)
            return real() if len(calls) == 1 else real() + 1      # the first call is the parent check of the preflight

        with mock.patch.object(install.os, "geteuid", geteuid):
            report = self.init()
        self.assertEqual(report.exit_code(), 3, report.lines)
        self.assertEqual(report.unverified, ["WRITE_FAILED"])
        self.assertIn("EPERM", self.lines(report))
        self.assertFalse(self.target.exists())
        self.assertEqual(os.listdir(self.staging_path()), [])                  # nothing was written into it
        self.assertTrue(any(l.startswith(f"LEFT_IN_PLACE: {self.staging_path()} (") for l in report.lines), report.lines)      # the full path
        self.assertNoSecrets(report)


@linux
class R1CleanupRaceTests(Base):
    """The cleanup empties only what this run created, removes no name, and says so when it cannot prove that."""

    def failing_run(self, wrap_name, wrapper_factory):
        """A run that fails at record_written, with `os.<wrap_name>` wrapped (ftruncate is called by the cleanup only)."""
        real = getattr(os, wrap_name)

        def fault(step):
            if step == "record_written":
                raise OSError(errno.EIO, "injected")

        with mock.patch.object(install.os, wrap_name, wrapper_factory(real)):
            return self.init(fault=fault)

    def test_a_swap_during_the_emptying_of_our_files_does_not_reach_the_foreign_directory(self):
        decoy = self.parent / "decoy"
        decoy.mkdir(mode=0o755)
        for name in FILES:
            (decoy / name).write_text("foreign " + name)
        before = tree(decoy)
        done = []

        def wrapper(real):
            def ftruncate(fd, *a, **kw):
                real(fd, *a, **kw)
                if not done:
                    done.append(1)
                    staging = self.staging_path()
                    os.rename(staging, self.parent / "moved-staging")
                    os.symlink(decoy, staging)                    # the name now leads to a directory holding files with our names
            return ftruncate

        report = self.failing_run("ftruncate", wrapper)
        self.assertEqual(report.exit_code(), 3, report.lines)
        self.assertEqual(tree(decoy), before)
        line = self.assertAnnounced(report, self.staging_path())
        self.assertIn(str(self.parent / "moved-staging"), line)
        self.assertEmptied(tree(self.parent / "moved-staging"))      # our files were emptied in the directory we created

    def test_a_failure_leaves_only_the_announced_emptied_staging(self):
        before = fx.snapshot(self.parent)

        def fault(step):
            if step == "env_written":
                raise OSError(errno.ENOSPC, "injected")

        report = self.init(fault=fault)
        self.assertEqual(report.unverified, ["WRITE_FAILED"], report.lines)
        self.assertEqual(fx.snapshot(self.parent), before)
        self.assertFalse(any(l.startswith("LEFT_IN_PLACE") for l in report.lines), report.lines)
        self.assertEqual(self.staging_names(), [])
        self.assertEmptied(tree(self.staging_path()), [".env"])              # the run failed right after the first file
        self.assertAnnounced(report, self.staging_path())


@linux
class R1OwnershipAndModeTests(Base):
    """Posture of the staging directory and of the published set, whatever the inherited umask or parent."""

    def test_modes_and_owner_hold_at_every_step_under_any_umask(self):
        for mask in (0o000, 0o022, 0o077, 0o277, 0o777):
            self.fresh_parent()
            problems = []

            def fault(step):
                for name in self.staging_names():
                    path = self.parent / name
                    info = os.lstat(path)
                    if stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.geteuid():
                        problems.append((step, "dir", oct(info.st_mode)))
                    for entry in os.listdir(path):
                        mode = stat.S_IMODE(os.lstat(path / entry).st_mode)
                        if mode != 0o600:
                            problems.append((step, entry, oct(mode)))

            old = os.umask(mask)
            try:
                report = self.init(fault=fault)
            finally:
                os.umask(old)
            with self.subTest(umask=oct(mask)):
                self.assertFalse(report.failed, report.lines)
                self.assertEqual(problems, [])
                self.assertPublishedAndComplete()

    def test_a_sticky_world_writable_parent_is_accepted_and_the_set_is_ours_and_private(self):
        os.chmod(self.parent, 0o1777)
        report = self.init()
        self.assertFalse(report.failed, report.lines)
        self.assertPublishedAndComplete()
        self.assertEqual(self.staging_names(), [])

    def test_a_parent_writable_by_others_without_the_sticky_bit_is_refused_before_anything_is_created(self):
        os.chmod(self.parent, 0o777)
        before = fx.snapshot(self.parent)
        report = self.init()
        self.assertEqual(report.refused, ["TARGET_PARENT_WRITABLE_BY_OTHERS"])
        self.assertEqual(fx.snapshot(self.parent), before)


class R1SourceScanTests(unittest.TestCase):
    """What install.py may call at all, read from its source: nothing replaces a name and nothing removes one (runs on every platform)."""

    DELETING = {"unlink", "remove", "rmdir", "rmtree", "removedirs", "unlinkat"}      # on whatever object: os, shutil, pathlib.Path

    def source(self):
        return ast.parse(Path(install.__file__).read_text(encoding="utf-8"))

    def calls(self):
        found = []
        for fn in ast.walk(self.source()):
            if isinstance(fn, ast.FunctionDef):
                for node in ast.walk(fn):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                        found.append((fn.name, node.func.value.id, node.func.attr))
        return found

    def test_the_source_never_calls_a_replacing_rename(self):
        banned = {("os", "rename"), ("os", "replace"), ("os", "renames"), ("shutil", "move")}
        self.assertEqual([c for c in self.calls() if c[1:] in banned], [])

    def test_the_source_never_removes_a_name_by_any_route(self):
        """No os.unlink, os.remove, os.rmdir, shutil.rmtree, Path.unlink and the like, however spelled: a call, an attribute, a bare or
        imported name, a getattr string, or the shutil module itself. The cleanup empties files through descriptors and removes no name."""
        found = []
        for node in ast.walk(self.source()):
            if isinstance(node, ast.Attribute) and node.attr in self.DELETING:
                found.append(("attribute", node.attr, node.lineno))
            elif isinstance(node, ast.Name) and node.id in self.DELETING:
                found.append(("name", node.id, node.lineno))
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in self.DELETING:
                found.append(("string", node.value, node.lineno))
            elif isinstance(node, ast.ImportFrom):
                found += [("import", a.name, node.lineno) for a in node.names if a.name in self.DELETING or node.module == "shutil"]
            elif isinstance(node, ast.Import):
                found += [("import", a.name, node.lineno) for a in node.names if a.name == "shutil"]
        self.assertEqual(found, [])

    def test_nothing_else_deletes_for_it_and_git_is_the_only_program_started(self):
        """No self-cleaning temporary file or directory, no shell, no exec or spawn; the one child process is git, started in _git."""
        banned = {("tempfile", "TemporaryDirectory"), ("tempfile", "NamedTemporaryFile"), ("tempfile", "SpooledTemporaryFile"),
                  ("os", "system"), ("os", "popen")}
        calls = self.calls()
        self.assertEqual([c for c in calls if c[1:] in banned or (c[1] == "os" and c[2].startswith(("exec", "spawn")))], [])
        self.assertEqual({name for name, owner, attr in calls if owner == "subprocess"}, {"_git"})


@linux
class R1NoPlainRenameTests(Base):
    """There is no fallback to a rename that replaces an existing target."""

    def test_a_filesystem_that_cannot_refuse_to_replace_leaves_everything_alone_and_never_falls_back(self):
        def boom(*a, **kw):
            raise AssertionError("a plain rename was attempted")

        for code in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.ENOTSUP):
            self.fresh_parent()
            before = fx.snapshot(self.parent)
            with mock.patch.object(install, "_rename_noreplace", side_effect=OSError(code, "no")), \
                    mock.patch.object(os, "rename", boom), mock.patch.object(os, "replace", boom), mock.patch.object(shutil, "move", boom):
                report = self.init()
            with self.subTest(errno=errno.errorcode[code]):
                self.assertEqual(report.unverified, ["PUBLISH_NOREPLACE_UNSUPPORTED"], report.lines)
                self.assertEqual(fx.snapshot(self.parent), before)

    def test_publication_and_a_refusal_use_no_plain_rename_at_all(self):
        def boom(*a, **kw):
            raise AssertionError("a plain rename was attempted")

        with mock.patch.object(os, "rename", boom), mock.patch.object(os, "replace", boom), mock.patch.object(shutil, "move", boom):
            self.assertFalse(self.init().failed)
            self.fresh_parent()
            make_empty_dir(self.target)
            self.assertEqual(self.init().refused, ["TARGET_EXISTS_EMPTY_DIRECTORY"])


@linux
class R4InProcessSignalTests(Base):
    """A real signal delivered to this process at the instants around the rename; the result comes from the disk."""

    def setUp(self):
        super().setUp()
        previous = signal.signal(signal.SIGINT, signal.default_int_handler)      # a runner started in the background may ignore SIGINT
        self.addCleanup(signal.signal, signal.SIGINT, previous)

    def after_syscall(self, signum):
        real = install._rename_noreplace

        def rename(src, dst, *rest):
            real(src, dst, *rest)
            os.kill(os.getpid(), signum)
        return mock.patch.object(install, "_rename_noreplace", rename)

    def in_parent_fsync(self, signum):
        real, calls = install._fsync_dir, []

        def fsync_dir(*args, **kw):
            real(*args, **kw)
            calls.append(1)
            if len(calls) == 2:
                os.kill(os.getpid(), signum)
        return mock.patch.object(install, "_fsync_dir", fsync_dir)

    def at_renamed_step(self, signum):
        return lambda step: os.kill(os.getpid(), signum) if step == "renamed" else None

    def assertCompleteAndTruthful(self, report):
        self.assertEqual(report.exit_code(), 0, report.lines)
        self.assertIn("CONFIG_PREPARED", report.lines)
        self.assertTrue(any(l.startswith("INTERRUPTED_AFTER_PUBLICATION") for l in report.lines), report.lines)
        self.assertFalse(any("nothing published" in l for l in report.lines), report.lines)
        self.assertFalse(any(l.startswith("LEFT_IN_PLACE") for l in report.lines), report.lines)
        self.assertPublishedAndComplete()
        self.assertEqual(self.staging_names(), [])
        self.assertNoSecrets(report)

    def test_a_signal_right_after_the_successful_rename_is_reported_from_the_disk(self):
        windows = {"after_syscall": lambda s: (self.after_syscall(s), None),
                   "parent_fsync": lambda s: (self.in_parent_fsync(s), None),
                   "renamed_step": lambda s: (contextlib.nullcontext(), self.at_renamed_step(s))}
        for window, make in windows.items():
            for signum in (signal.SIGINT, signal.SIGTERM):
                self.fresh_parent()
                patch, fault = make(signum)
                with patch:
                    report = self.init(fault=fault) if fault else self.init()
                with self.subTest(window=window, signal=signum.name):
                    self.assertCompleteAndTruthful(report)
                    # a second run must neither regenerate the keys nor change a byte
                    before = (fx.snapshot(self.parent), (self.target / ".env").read_bytes())
                    again = self.init(gen={k: (lambda: "x") for k in install.GENERATED})
                    self.assertEqual(again.refused, ["TARGET_EXISTS_CONFIG_SET"], again.lines)
                    self.assertEqual(fx.field(again.lines, "EXISTING_KEY_FINGERPRINT"), "MATCH")
                    self.assertEqual((fx.snapshot(self.parent), (self.target / ".env").read_bytes()), before)

    def test_a_signal_just_before_the_rename_publishes_nothing_and_says_so(self):
        real = install._rename_noreplace

        def rename(src, dst, *rest):
            os.kill(os.getpid(), signal.SIGTERM)
            real(src, dst, *rest)                                 # not reached: the handler raises first

        before = fx.snapshot(self.parent)
        with mock.patch.object(install, "_rename_noreplace", rename):
            report = self.init()
        self.assertEqual(report.exit_code(), 3, report.lines)
        self.assertEqual(report.unverified, ["INTERRUPTED"])
        self.assertTrue(any("nothing published" in l for l in report.lines), report.lines)
        self.assertEqual(fx.snapshot(self.parent), before)

    def test_an_unexpected_exception_at_any_step_still_cleans_up_and_publishes_nothing(self):
        before = fx.snapshot(self.parent)
        for step in ("begin", "staged_dir", "env_written", "record_written", "before_publish"):
            def fault(s, step=step):
                if s == step:
                    raise RuntimeError("an unexpected bug with " + fx.STUB_VALUES["POSTGRES_PASSWORD"])

            report = self.init(fault=fault)
            with self.subTest(step=step):
                self.assertEqual(report.exit_code(), 3, report.lines)
                self.assertEqual(report.unverified, ["WRITE_FAILED"])
                self.assertEqual(fx.snapshot(self.parent), before)
                self.assertNoSecrets(report)

    def test_an_unexpected_exception_after_the_rename_cannot_unpublish_or_deny_the_set(self):
        def fault(step):
            if step == "renamed":
                raise RuntimeError("an unexpected bug")

        report = self.init(fault=fault)
        self.assertIn("CONFIG_PREPARED", report.lines, report.lines)
        self.assertFalse(any("nothing published" in l for l in report.lines), report.lines)
        self.assertPublishedAndComplete()

    def test_an_interrupt_while_the_state_is_being_determined_is_admitted_not_hidden(self):
        real_state = install._publication_state

        def state_then_interrupt():
            calls = []

            def state(*args):
                calls.append(1)
                if len(calls) > 1:                                  # the first call is the check before the rename
                    raise KeyboardInterrupt
                return real_state(*args)
            return state

        def rename(src, dst, *rest):
            raise OSError(errno.EIO, "injected")

        for victim, replacement in (("_publication_state", state_then_interrupt()), ("_discard", mock.Mock(side_effect=KeyboardInterrupt))):
            self.fresh_parent()
            with mock.patch.object(install, "_rename_noreplace", rename), mock.patch.object(install, victim, replacement):
                report = self.init()
            with self.subTest(victim=victim):
                self.assertEqual(report.exit_code(), 3, report.lines)
                self.assertEqual(report.unverified, ["INTERRUPTED_WHILE_FINISHING"], report.lines)
                self.assertNotIn("CONFIG_PREPARED", report.lines)
                self.assertFalse(any("nothing published" in l for l in report.lines), report.lines)
                for name in os.listdir(self.parent):                # what may still hold the unpublished secrets is named, by its full path
                    if ".staging-" in name:
                        self.assertIn(str(self.parent / name), self.lines(report))


@linux
class R4ExternalSignalTests(Base):
    """A real child process held at one instant of init-config and sent a real SIGINT or SIGTERM from outside."""

    def child(self, window, signum, between=None):
        ready = self.tmp / f"ready_{window}_{signum.name}"
        proc = subprocess.Popen([sys.executable, "-c", CHILD_CODE, self.sha, str(self.target), str(self.repo)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env={**os.environ, "TESTS": TESTS, "WINDOW": window, "READY": str(ready)})
        deadline = time.time() + 30
        while not ready.exists():
            if proc.poll() is not None:
                self.fail(f"the child ended before the {window} instant: {proc.communicate()}")
            self.assertLess(time.time(), deadline, "the child never reached the instant")
            time.sleep(0.01)
        if between:
            between()
        os.kill(proc.pid, signum)
        out, err = proc.communicate(timeout=60)
        return proc.returncode, out, err

    def lines_of(self, out):
        return out.splitlines()

    def assertMarkers(self, out):
        self.assertEqual(self.lines_of(out)[-2:], list(install.MARKERS))

    def assertNoSecretsIn(self, *texts):
        for value in fx.STUB_VALUES.values():
            for text in texts:
                self.assertNotIn(value, text)

    def test_a_real_signal_after_the_rename_reports_the_complete_set_and_a_rerun_agrees(self):
        for window in ("after_syscall", "parent_fsync"):
            for signum in (signal.SIGINT, signal.SIGTERM):
                self.fresh_parent()
                code, out, err = self.child(window, signum)
                with self.subTest(window=window, signal=signum.name):
                    self.assertEqual(code, 0, (out, err))
                    self.assertIn("CONFIG_PREPARED", self.lines_of(out))
                    self.assertTrue(any(l.startswith("INTERRUPTED_AFTER_PUBLICATION") for l in self.lines_of(out)), out)
                    self.assertNotIn("nothing published", out)
                    self.assertNotIn("LEFT_IN_PLACE", out)
                    self.assertNotIn("Traceback", err)
                    self.assertMarkers(out)
                    self.assertNoSecretsIn(out, err)
                    self.assertPublishedAndComplete()
                    self.assertEqual(self.staging_names(), [])
                    before = (fx.snapshot(self.parent), (self.target / ".env").read_bytes())
                    code, lines = fx.cli("init-config", self.sha, self.target, self.repo)                # real generators
                    self.assertEqual((code, fx.field(lines, "EXISTING_KEY_FINGERPRINT")), (2, "MATCH"), lines)
                    self.assertTrue(any(l.startswith("REFUSED TARGET_EXISTS_CONFIG_SET") for l in lines), lines)
                    self.assertEqual((fx.snapshot(self.parent), (self.target / ".env").read_bytes()), before)

    def test_a_real_signal_just_before_the_rename_publishes_nothing_and_leaves_no_staging(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            self.fresh_parent()
            before = fx.snapshot(self.parent)
            code, out, err = self.child("before_syscall", signum)
            with self.subTest(signal=signum.name):
                self.assertEqual(code, 3, (out, err))
                self.assertIn("UNVERIFIED INTERRUPTED: nothing published", out)
                self.assertNotIn("CONFIG_PREPARED", out)
                self.assertNotIn("Traceback", err)
                self.assertMarkers(out)
                self.assertEqual(fx.snapshot(self.parent), before)
                self.assertNoSecretsIn(out, err)
                code, lines = fx.cli("init-config", self.sha, self.target, self.repo)                # nothing was published: a normal run
                self.assertEqual(code, 0, lines)

    def test_a_real_signal_after_the_rename_with_the_target_swapped_is_unverified_and_nothing_is_removed(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            self.fresh_parent()
            moved = self.parent / "ours-moved"
            seen = {}

            def swap():
                os.rename(self.target, moved)
                make_dir_with_content(self.target)
                seen.update(tree(self.target))

            code, out, err = self.child("after_syscall", signum, between=swap)
            with self.subTest(signal=signum.name):
                self.assertEqual(code, 3, (out, err))
                self.assertIn("UNVERIFIED PUBLICATION_STATE_UNKNOWN", out)
                self.assertNotIn("CONFIG_PREPARED", out)
                self.assertNotIn("nothing published", out)
                self.assertIn(os.path.realpath(moved), out)
                self.assertNotIn("Traceback", err)
                self.assertMarkers(out)
                self.assertEqual(tree(self.target), seen)
                self.assertEqual(sorted(os.listdir(moved)), sorted(FILES))
                before = fx.snapshot(self.parent)
                code, lines = fx.cli("init-config", self.sha, self.target, self.repo)                # the foreign directory blocks a rerun
                self.assertEqual(code, 2, lines)
                self.assertEqual(fx.snapshot(self.parent), before)

    def test_a_real_sigkill_after_the_rename_leaves_the_complete_set_and_a_rerun_refuses_it_unchanged(self):
        code, out, err = self.child("after_syscall", signal.SIGKILL)
        self.assertEqual(code, -signal.SIGKILL, (out, err))
        self.assertPublishedAndComplete()
        self.assertEqual(self.staging_names(), [])
        before = (fx.snapshot(self.parent), (self.target / ".env").read_bytes())
        code, lines = fx.cli("init-config", self.sha, self.target, self.repo)
        self.assertEqual((code, fx.field(lines, "EXISTING_KEY_FINGERPRINT")), (2, "MATCH"), lines)
        self.assertEqual((fx.snapshot(self.parent), (self.target / ".env").read_bytes()), before)

    def test_a_real_sigkill_before_the_rename_leaves_one_staging_that_the_next_run_reports_and_never_uses(self):
        code, out, err = self.child("before_syscall", signal.SIGKILL)
        self.assertEqual(code, -signal.SIGKILL, (out, err))
        self.assertFalse(self.target.exists())
        (stale,) = self.staging_names()
        stale_before = fx.snapshot(self.parent / stale)
        self.assertEqual(sorted(stale_before), sorted(FILES))
        code, lines = fx.cli("init-config", self.sha, self.target, self.repo)
        self.assertEqual(code, 0, lines)
        self.assertTrue(any(l.startswith("STALE_STAGING") and stale in l for l in lines), lines)      # reported, not silent
        self.assertEqual(fx.snapshot(self.parent / stale), stale_before)                              # never deleted, never reused
        self.assertNotEqual((self.target / ".env").read_bytes(), (self.parent / stale / ".env").read_bytes())

    def test_a_second_real_signal_during_the_cleanup_is_admitted_and_never_kills_the_run_silently(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            self.fresh_parent()
            code, out, err = self.child("during_cleanup", signum)
            with self.subTest(signal=signum.name):
                self.assertEqual(code, 3, (code, out, err))
                self.assertIn("UNVERIFIED INTERRUPTED_WHILE_FINISHING", out)
                self.assertNotIn("CONFIG_PREPARED", out)
                self.assertNotIn("Traceback", err)
                self.assertMarkers(out)
                (stale,) = [n for n in os.listdir(self.parent) if ".staging-" in n]
                self.assertIn(str(self.parent / stale), out)                                           # the leftover is named by its full path
                self.assertEqual(self.staging_names(), [], "the second interrupt left our secrets in the staging directory")
                self.assertEmptied(tree(self.parent / stale))
                self.assertIn("2 files emptied", out)
                self.assertNoSecretsIn(out, err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
