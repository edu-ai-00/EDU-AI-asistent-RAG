"""B06-1, Codex final review, finding 1: the cleanup has no by-name step between a check and a removal.

The old cleanup looked at each file it had created inside the held staging directory (stat, inode compared) and then unlinked the NAME.
A second process could rename our file away and put a foreign file under the same name between the two calls, and the unlink deleted
the foreign one. Now no name is removed at all: the run keeps every file it creates open and empties exactly that inode through the
descriptor (ftruncate), so there is no by-name step left to race, and the staging directory stays, announced with its full path and state.

Run: python tests/test_install_discard_race.py   (Linux; standard library + git; synthetic markers and temporary directories only)
The second process is a real fork. Races are made deterministic by wrapping the os-level calls of the cleanup: the swap is done by that
other process immediately BEFORE the n-th call, for every n, and the invariants are checked each time.
"""
import contextlib
import errno
import hashlib
import multiprocessing
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import install_fixtures as fx  # noqa: E402
from install_fixtures import install  # noqa: E402

linux = unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux (fork, /proc, modes, renameat2)")
FILES = [".env", "install-record.json"]
SECRETS = (b"SECRET_ENV_MARKER_7f3a\n", b"SECRET_RECORD_MARKER_91cc\n")
FOREIGN_ENV, FOREIGN_RECORD = b"FOREIGN_ENV_MARKER_c0de\n", b"FOREIGN_RECORD_MARKER_5eed\n"
KINDS = ("rename_aside", "delete_and_recreate", "symlink_takes_the_name", "directory_takes_the_name", "fifo_takes_the_name",
         "staging_directory_swapped", "staging_replaced_by_a_link", "a_second_name_for_our_file")
# every os-level call the cleanup could make, whatever it does (a name in this list that the code never calls is simply not counted)
OS_CALLS = ("open", "stat", "lstat", "fstat", "ftruncate", "fsync", "listdir", "scandir", "readlink", "close", "unlink", "remove", "rmdir",
            "rename", "replace", "link", "symlink", "chmod", "fchmod", "utime", "write", "read", "mkdir", "truncate", "access", "dup")


def fingerprint(path):
    """Everything observable about an object, links not followed: kind, device, inode, mode, size, mtime, and content or target or children."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "MISSING"
    entry = [stat.S_IFMT(info.st_mode), info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode), info.st_size, info.st_mtime_ns]
    if stat.S_ISLNK(info.st_mode):
        entry.append(os.readlink(path))
    elif stat.S_ISREG(info.st_mode):
        entry.append(hashlib.sha256(Path(path).read_bytes()).hexdigest())
    elif stat.S_ISDIR(info.st_mode):
        entry.append({name: fingerprint(os.path.join(path, name)) for name in sorted(os.listdir(path))})
    return entry


def swap(kind, staging, parent):
    """What the second process does to the staging directory it knows by path. Returns {path: fingerprint} of every foreign object it leaves."""
    env, record = os.path.join(staging, ".env"), os.path.join(staging, "install-record.json")
    aside, moved, made = os.path.join(staging, "owned-saved"), os.path.join(parent, "moved-staging"), []

    def foreign(path, data=FOREIGN_ENV):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        os.write(fd, data)
        os.fchmod(fd, 0o640)
        os.close(fd)
        made.append(path)

    if kind == "rename_aside":                                   # the Codex reproduction: our file renamed away, a foreign one under its name
        os.rename(env, aside)
        foreign(env)
    elif kind == "delete_and_recreate":                          # an attempt to get our inode numbers again for foreign files
        os.unlink(env)
        foreign(env)
        os.unlink(record)
        foreign(record, FOREIGN_RECORD)
    elif kind == "symlink_takes_the_name":
        outside = os.path.join(parent, "outside.txt")
        foreign(outside)
        os.rename(env, aside)
        os.symlink(outside, env)
        made.append(env)
    elif kind == "directory_takes_the_name":
        os.rename(env, aside)
        os.mkdir(env, 0o750)
        foreign(os.path.join(env, "inner.txt"))
        made.append(env)
    elif kind == "fifo_takes_the_name":
        os.rename(env, aside)
        os.mkfifo(env, 0o640)
        made.append(env)
    elif kind == "staging_directory_swapped":                    # the whole directory moved, a foreign one with our file names at its name
        os.rename(staging, moved)
        os.mkdir(staging, 0o700)
        foreign(env)
        foreign(record, FOREIGN_RECORD)
        made.append(staging)
    elif kind == "staging_replaced_by_a_link":
        decoy = os.path.join(parent, "decoy")
        os.mkdir(decoy, 0o750)
        foreign(os.path.join(decoy, ".env"))
        foreign(os.path.join(decoy, "install-record.json"), FOREIGN_RECORD)
        os.rename(staging, moved)
        os.symlink(decoy, staging)
        made += [decoy, staging]
    elif kind == "a_second_name_for_our_file":                   # not foreign: our own inode under one more name
        os.link(env, os.path.join(parent, "second-name"))
    return {path: fingerprint(path) for path in made}


def other_process(conn):
    """The second process: forked before anything is patched, it does one swap per request and answers with what it made."""
    while True:
        job = conn.recv()
        if job is None:
            return
        try:
            conn.send(("ok", swap(*job)))
        except BaseException as exc:  # noqa: BLE001 - the answer is the test's evidence, whatever happened
            conn.send(("error", repr(exc)))


class Injector:
    """Wraps the os-level calls and, before the `target`-th call made while armed, has the second process swap (target 0 only counts)."""

    def __init__(self, other, kind, target, staging, parent):
        self.other, self.kind, self.target, self.staging, self.parent = other, kind, target, staging, parent
        self.n, self.seen, self.made, self.busy, self.armed = 0, [], None, False, False

    def wrap(self, name):
        real = getattr(os, name)

        def call(*args, **kw):
            if self.armed and not self.busy:
                self.n += 1
                self.seen.append(name)
                if self.n == self.target:
                    self.busy = True
                    try:
                        self.swap_now()
                    finally:
                        self.busy = False
            return real(*args, **kw)
        return call

    def swap_now(self):
        self.other.send((self.kind, self.staging, self.parent))
        if not self.other.poll(60):
            raise AssertionError("the second process did not answer")
        status, made = self.other.recv()
        if status != "ok":
            raise AssertionError(f"the second process failed: {made}")
        self.made = made

    @contextlib.contextmanager
    def active(self):
        with contextlib.ExitStack() as stack:
            for name in OS_CALLS:
                if hasattr(os, name):
                    stack.enter_context(mock.patch.object(os, name, self.wrap(name)))
            self.armed = True
            try:
                yield self
            finally:
                self.armed = False


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ctx = multiprocessing.get_context("fork")
        cls.other, child = ctx.Pipe()
        cls.proc = ctx.Process(target=other_process, args=(child,), daemon=True)
        cls.proc.start()

    @classmethod
    def tearDownClass(cls):
        cls.other.send(None)
        cls.proc.join(30)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(os.path.realpath(self._tmp.name))      # the kernel reports resolved paths, and so does the code under test
        self.repo, self.sha = fx.make_repo(self.tmp / "repo")
        self.fresh_parent()

    def fresh_parent(self):
        self.parent = Path(tempfile.mkdtemp(dir=self.tmp, prefix="cfg_"))
        os.chmod(self.parent, 0o700)
        self.target = self.parent / "set"

    def make_run(self):
        """A run exactly as init_config builds one: the staging directory pinned by descriptor, both files created and held, secrets written."""
        run = install._Run()
        install._create_staging(run, str(self.parent), "set")
        install._write_private(install.ENV_NAME, SECRETS[0], run.fd, run.held)
        install._write_private(install.RECORD_NAME, SECRETS[1], run.fd, run.held)
        self.addCleanup(install._release, run)
        return run

    def init(self, **kw):
        kw.setdefault("gen", fx.STUB_GEN)
        try:
            return install.init_config(self.sha, str(self.target), str(self.repo), **kw)
        except KeyboardInterrupt:
            self.fail("an interrupt escaped init_config")

    @staticmethod
    def fail_at(step, exc=None):
        def fault(s):
            if s == step:
                raise exc or OSError(errno.EIO, "injected")
        return fault

    def staging_dirs(self):
        return [self.parent / n for n in sorted(os.listdir(self.parent)) if ".staging-" in n]

    def assertNoSecretOnDisk(self):
        """No regular file anywhere under the test root holds one of our secret markers (links are not followed, FIFOs not opened)."""
        for dirpath, _, names in os.walk(self.tmp, followlinks=False):
            for name in names:
                path = os.path.join(dirpath, name)
                if stat.S_ISREG(os.lstat(path).st_mode):
                    data = Path(path).read_bytes()
                    for secret in SECRETS:
                        self.assertNotIn(secret.strip(), data, path)

    def assertOursEmptied(self, run):
        """Every inode this run created is at zero length and holds no secret, read through the descriptor and not through any name."""
        for name, fd in run.held.items():
            self.assertEqual(os.fstat(fd).st_size, 0, name)
            self.assertEqual(Path(f"/proc/self/fd/{fd}").read_bytes(), b"", name)

    def announcement(self, report, run, token="STAGING_LEFT_EMPTIED"):
        (line,) = [l for l in report.lines if l.startswith(token + ": ")]
        self.assertTrue(os.path.isabs(run.staging))
        self.assertTrue(line.startswith(f"{token}: {run.staging} ("), line)
        return line


@linux
class EveryInstantOfTheCleanupTests(Base):
    """A second process swaps before EACH os-level call the cleanup makes; no foreign object is removed or changed, our secrets are gone."""

    def discard_with_swap(self, kind, target):
        self.fresh_parent()
        run = self.make_run()
        report = install.Report()
        injector = Injector(self.other, kind, target, run.staging, str(self.parent))
        with injector.active():
            install._discard(run, report)
        return run, report, injector

    def test_the_instants_are_really_there_and_cover_the_calls_the_cleanup_makes(self):
        _, _, probe = self.discard_with_swap("rename_aside", 0)
        self.assertGreaterEqual(probe.n, 8, probe.seen)
        self.assertLessEqual({"ftruncate", "fstat", "fsync", "listdir", "stat", "readlink"}, set(probe.seen), probe.seen)
        self.assertEqual(set(probe.seen) & {"unlink", "remove", "rmdir", "rename", "replace", "open", "truncate", "chmod", "utime", "link"}, set(),
                         "the cleanup must not remove, rename, open or change anything by name")

    def test_a_swap_before_each_call_never_reaches_a_foreign_object_and_always_empties_ours(self):
        _, _, probe = self.discard_with_swap("rename_aside", 0)
        total = probe.n
        swapped = 0
        for kind in KINDS:
            for position in range(1, total + 1):
                run, report, injector = self.discard_with_swap(kind, position)
                with self.subTest(kind=kind, before_call=position, call=injector.seen[position - 1]):
                    self.assertIsNotNone(injector.made, "the swap seam was not reached")
                    swapped += 1
                    for path, before in injector.made.items():
                        self.assertEqual(fingerprint(path), before, f"{path} was changed or removed by the cleanup")
                    ours = {(os.fstat(fd).st_dev, os.fstat(fd).st_ino) for fd in run.held.values()}
                    self.assertEqual([p for p, f in injector.made.items() if f != "MISSING" and (f[1], f[2]) in ours], [],
                                     "a foreign object got the inode of a file this run created")
                    self.assertOursEmptied(run)
                    self.assertNoSecretOnDisk()
                    line = self.announcement(report, run)
                    self.assertIn("2 of 2 files created by this run were emptied", line)
                    self.assertFalse(any(l.startswith(("LEFT_IN_PLACE", "STAGING_LEFT_NOT_EMPTIED")) for l in report.lines), report.lines)
                install._release(run)
        self.assertEqual(swapped, len(KINDS) * total)

    def test_the_codex_scenario_end_to_end(self):
        """Our .env renamed away and a foreign .env put under its name by a real second process, while a failed run cleans up."""
        def fault(step):
            if step == "record_written":
                raise OSError(errno.EIO, "injected")

        # The swap comes right after the first by-name stat of .env (the instant of Codex's reproduction), or before the first ftruncate.
        injector = Injector(self.other, "rename_aside", 0, "", str(self.parent))
        real_stat, real_ftruncate = os.stat, os.ftruncate

        def swap_once(staging):
            if injector.made is None:
                injector.staging = staging
                injector.swap_now()

        def stat_(path, *a, **kw):
            result = real_stat(path, *a, **kw)
            if path == ".env" and kw.get("dir_fd") is not None and injector.made is None:
                swap_once(self.staging_dirs()[0].as_posix())
            return result

        def ftruncate(fd, *a, **kw):
            if injector.made is None:
                swap_once(self.staging_dirs()[0].as_posix())
            return real_ftruncate(fd, *a, **kw)

        with mock.patch.object(os, "stat", stat_), mock.patch.object(os, "ftruncate", ftruncate):
            report = self.init(fault=fault)
        self.assertIsNotNone(injector.made, "the second process never swapped")
        (staging,) = self.staging_dirs()
        foreign = staging / ".env"
        self.assertEqual(foreign.read_bytes(), FOREIGN_ENV)                                # the foreign file is byte-identical ...
        self.assertEqual(fingerprint(foreign), injector.made[foreign.as_posix()])         # ... same inode, same mode, same mtime
        self.assertEqual(stat.S_IMODE(os.lstat(foreign).st_mode), 0o640)
        self.assertEqual([(staging / n).stat().st_size for n in ("owned-saved", "install-record.json")], [0, 0])    # ours: zero bytes
        self.assertEqual((report.exit_code(), report.unverified), (3, ["WRITE_FAILED"]), report.lines)
        (line,) = [l for l in report.lines if l.startswith("STAGING_LEFT_EMPTIED: ")]
        self.assertTrue(line.startswith(f"STAGING_LEFT_EMPTIED: {staging} ("), line)    # the full path, and the state in the same line
        self.assertIn("2 of 2 files created by this run were emptied", line)
        self.assertIn("1 other entry is present and left untouched", line)
        self.assertNotIn("LEFT_IN_PLACE", "\n".join(report.lines))
        self.assertNoSecretOnDisk()
        for secret in SECRETS + (FOREIGN_ENV,):
            self.assertNotIn(secret.strip().decode(), "\n".join(report.lines))


@linux
class TheNameIsNotTheIdentityTests(Base):
    """Delete-and-recreate, a link, a directory or a FIFO under our file names: whatever is there is not ours, and stays as it was."""

    def run_then_swap_then_discard(self, kind):
        run = self.make_run()
        self.other.send((kind, run.staging, str(self.parent)))
        status, made = self.other.recv()
        self.assertEqual(status, "ok", made)
        report = install.Report()
        install._discard(run, report)
        return run, report, made

    def test_a_deleted_and_recreated_name_never_gets_the_inode_of_a_file_this_run_holds(self):
        run = self.make_run()
        ours = {name: os.fstat(fd).st_ino for name, fd in run.held.items()}
        self.other.send(("delete_and_recreate", run.staging, str(self.parent)))
        status, made = self.other.recv()
        self.assertEqual(status, "ok", made)
        for i in range(300):                                                                # many more creations: none may get an inode of ours
            path = Path(run.staging) / f"filler-{i}"
            path.write_bytes(b"x")
            self.assertNotIn(os.lstat(path).st_ino, ours.values())
        for path in made:
            self.assertNotIn(fingerprint(path)[2], ours.values())
        report = install.Report()
        install._discard(run, report)
        self.assertEqual({p: fingerprint(p) for p in made}, made)
        self.assertEqual(Path(run.staging, ".env").read_bytes(), FOREIGN_ENV)
        self.assertEqual(Path(run.staging, "install-record.json").read_bytes(), FOREIGN_RECORD)
        self.assertOursEmptied(run)                                                          # the unlinked inodes are emptied all the same
        self.assertIn("302 other entries are present and left untouched", self.announcement(report, run))

    def test_a_foreign_symlink_in_place_of_our_file_is_not_followed_or_changed(self):
        run, report, made = self.run_then_swap_then_discard("symlink_takes_the_name")
        self.assertEqual({p: fingerprint(p) for p in made}, made)
        self.assertEqual(Path(run.staging, ".env").read_bytes(), FOREIGN_ENV)                  # the link still leads to the foreign file
        self.assertEqual(Path(run.staging, "owned-saved").stat().st_size, 0)
        self.assertOursEmptied(run)
        self.assertIn("1 other entry is present and left untouched", self.announcement(report, run))

    def test_a_foreign_directory_in_place_of_our_file_is_not_entered_or_changed(self):
        run, report, made = self.run_then_swap_then_discard("directory_takes_the_name")
        self.assertEqual({p: fingerprint(p) for p in made}, made)
        self.assertEqual(Path(run.staging, ".env", "inner.txt").read_bytes(), FOREIGN_ENV)
        self.assertOursEmptied(run)
        self.assertIn("1 other entry is present and left untouched", self.announcement(report, run))

    def test_a_fifo_in_place_of_our_file_does_not_hang_the_cleanup(self):
        run, report, made = self.run_then_swap_then_discard("fifo_takes_the_name")
        self.assertTrue(stat.S_ISFIFO(os.lstat(Path(run.staging, ".env")).st_mode))
        self.assertOursEmptied(run)
        self.assertIn("1 other entry is present and left untouched", self.announcement(report, run))

    def test_a_second_name_for_our_file_leads_to_an_emptied_file_too(self):
        run, report, _ = self.run_then_swap_then_discard("a_second_name_for_our_file")
        self.assertEqual(Path(self.parent, "second-name").read_bytes(), b"")                  # the same inode, so the same emptiness
        self.assertNoSecretOnDisk()
        self.assertIn("0 other entries are present and left untouched", self.announcement(report, run))

    def test_the_directory_swapped_away_is_found_again_through_its_descriptor(self):
        run, report, made = self.run_then_swap_then_discard("staging_directory_swapped")
        moved = self.parent / "moved-staging"
        self.assertEqual({p: fingerprint(p) for p in made}, made)
        self.assertEqual([(moved / n).stat().st_size for n in FILES], [0, 0])
        line = self.announcement(report, run)
        self.assertIn(f"the directory is now at {moved}", line)
        self.assertIn("0 other entries are present", line)                                     # what the report counts is the directory we hold
        self.assertEqual(Path(run.staging, ".env").read_bytes(), FOREIGN_ENV)

    def test_a_link_in_place_of_the_directory_leads_nowhere(self):
        run, report, made = self.run_then_swap_then_discard("staging_replaced_by_a_link")
        self.assertEqual({p: fingerprint(p) for p in made}, made)
        self.assertEqual([(self.parent / "decoy" / n).read_bytes() for n in FILES], [FOREIGN_ENV, FOREIGN_RECORD])
        self.assertIn(f"the directory is now at {self.parent / 'moved-staging'}", self.announcement(report, run))
        self.assertNoSecretOnDisk()


@linux
class WhatTheReportSaysTests(Base):
    """The state of the leftover is stated, never guessed: emptied only when seen at zero length, otherwise plainly not."""

    def test_a_failing_ftruncate_is_reported_as_not_emptied_and_never_as_clean(self):
        run = self.make_run()
        report = install.Report()
        with mock.patch.object(os, "ftruncate", side_effect=OSError(errno.EIO, "injected")):
            install._discard(run, report)
        line = self.announcement(report, run, "STAGING_LEFT_NOT_EMPTIED")
        self.assertIn("2 of 2 files created by this run could NOT be emptied", line)
        self.assertIn("may still hold secrets", line)
        self.assertIn(f".env at {run.staging}/.env", line)                                     # where each of them is, by the kernel's own record
        self.assertIn("remove the directory yourself", line)
        self.assertFalse(any(l.startswith("STAGING_LEFT_EMPTIED") or "were emptied" in l for l in report.lines), report.lines)
        self.assertEqual(Path(run.staging, ".env").read_bytes(), SECRETS[0])                  # and the secret really is still there

    def test_one_failing_file_does_not_stop_the_other_from_being_emptied(self):
        run = self.make_run()
        report, real, calls = install.Report(), os.ftruncate, []

        def ftruncate(fd, *a, **kw):
            calls.append(fd)
            if len(calls) == 1:
                raise OSError(errno.EIO, "injected")
            return real(fd, *a, **kw)

        with mock.patch.object(os, "ftruncate", ftruncate):
            install._discard(run, report)
        self.assertEqual(len(calls), 2)
        line = self.announcement(report, run, "STAGING_LEFT_NOT_EMPTIED")
        self.assertIn("1 of 2 files created by this run could NOT be emptied", line)
        self.assertIn(f".env at {run.staging}/.env", line)
        self.assertNotIn("install-record.json at", line)
        self.assertEqual(Path(run.staging, "install-record.json").stat().st_size, 0)

    def test_a_truncation_that_returns_but_does_nothing_is_not_taken_for_emptied(self):
        run = self.make_run()
        report = install.Report()
        with mock.patch.object(os, "ftruncate", lambda *a, **kw: None):
            install._discard(run, report)
        self.announcement(report, run, "STAGING_LEFT_NOT_EMPTIED")
        self.assertFalse(any(l.startswith("STAGING_LEFT_EMPTIED") for l in report.lines), report.lines)

    def test_through_init_config_the_failure_is_the_same_and_still_exit_3_without_a_secret(self):
        with mock.patch.object(os, "ftruncate", side_effect=OSError(errno.EIO, "injected")):
            report = self.init(fault=self.fail_at("record_written"))
        (staging,) = self.staging_dirs()
        self.assertEqual((report.exit_code(), report.unverified), (3, ["WRITE_FAILED"]), report.lines)
        (line,) = [l for l in report.lines if l.startswith("STAGING_LEFT_NOT_EMPTIED: ")]
        self.assertTrue(line.startswith(f"STAGING_LEFT_NOT_EMPTIED: {staging} ("), line)
        for secret in SECRETS:
            self.assertNotIn(secret.strip().decode(), "\n".join(report.lines))

    def test_a_directory_this_run_never_vouched_for_is_left_in_place_with_its_full_path(self):
        run = install._Run(staging=str(self.parent / ".set.staging-never"), fd=None, ident=None)
        report = install.Report()
        install._discard(run, report)
        (line,) = report.lines
        self.assertTrue(line.startswith(f"LEFT_IN_PLACE: {run.staging} ("), line)
        self.assertIn("nothing in it was read, changed or removed", line)

    def test_the_line_is_the_same_wherever_the_run_ended_and_the_names_stay(self):
        """Each way a run can fail leaves the same state: our files emptied, nothing removed, one announcement with the full path."""
        for step in ("staged_dir", "env_written", "record_written", "before_publish"):
            self.fresh_parent()
            report = self.init(fault=self.fail_at(step, KeyboardInterrupt() if step == "env_written" else None))
            (staging,) = self.staging_dirs()
            with self.subTest(step=step):
                self.assertEqual(report.exit_code(), 3, report.lines)
                (line,) = [l for l in report.lines if l.startswith("STAGING_LEFT_EMPTIED: ")]
                self.assertTrue(line.startswith(f"STAGING_LEFT_EMPTIED: {staging} ("), line)
                self.assertEqual(fx.secret_staging(self.parent), [])
                self.assertEqual(len(os.listdir(staging)), {"staged_dir": 0, "env_written": 1}.get(step, 2))
                self.assertTrue(all(os.lstat(staging / n).st_size == 0 for n in os.listdir(staging)))
                self.assertNoSecretOnDisk()

    def test_a_failure_in_the_middle_of_a_write_leaves_no_partial_secret(self):
        real, calls, armed = os.write, [], []

        def write(fd, data):
            if not armed:
                return real(fd, data)
            calls.append(fd)
            if len(calls) == 1:
                return real(fd, bytes(data[:7]))                  # a short write ...
            raise OSError(errno.ENOSPC, "injected")              # ... and then the disk is full

        with mock.patch.object(os, "write", write):
            report = self.init(fault=lambda step: armed.append(1) if step == "staged_dir" else None)
        (staging,) = self.staging_dirs()
        self.assertEqual((report.exit_code(), report.unverified), (3, ["WRITE_FAILED"]), report.lines)
        self.assertEqual(os.lstat(staging / ".env").st_size, 0)                                # the 7 bytes already written are gone too
        self.assertTrue(any(l.startswith(f"STAGING_LEFT_EMPTIED: {staging} (") and "1 of 1 files" in l for l in report.lines), report.lines)


@linux
class DescriptorsAndNamesTests(Base):
    """Every descriptor the run holds is closed on every exit path, and no run removes a name."""

    DELETIONS = ("os.remove", "os.rmdir")

    @staticmethod
    def open_descriptors():
        return sorted(int(n) for n in os.listdir("/proc/self/fd"))

    def failures(self):
        out = {}
        for step in ("begin", "staged_dir", "env_written", "record_written", "before_publish", "renamed"):
            out[f"error at {step}"] = dict(fault=self.fail_at(step))
            out[f"interrupt at {step}"] = dict(fault=self.fail_at(step, KeyboardInterrupt()))
        out["success"] = {}
        return out

    def test_no_descriptor_survives_any_exit_path(self):
        self.init()                                              # warm-up: lazily opened descriptors of the interpreter are not leaks
        self.fresh_parent()
        for label, kw in self.failures().items():
            self.fresh_parent()
            before = self.open_descriptors()
            report = self.init(**kw)
            with self.subTest(path=label):
                self.assertEqual(self.open_descriptors(), before, report.lines)

    def test_no_descriptor_survives_a_failing_truncation_a_failing_close_or_a_second_interrupt(self):
        cases = {
            "ftruncate fails": mock.patch.object(os, "ftruncate", side_effect=OSError(errno.EIO, "injected")),
            "discard interrupted": mock.patch.object(install, "_discard", side_effect=KeyboardInterrupt),
            "fsync of a file fails": mock.patch.object(os, "fsync", side_effect=OSError(errno.EIO, "injected")),
        }
        for label, patcher in cases.items():
            self.fresh_parent()
            before = self.open_descriptors()
            with patcher:
                report = self.init(fault=self.fail_at("record_written"))
            with self.subTest(path=label):
                self.assertEqual(self.open_descriptors(), before, report.lines)

    def test_a_close_that_fails_does_not_keep_the_other_descriptors_open(self):
        run = self.make_run()
        fds = [*run.held.values(), run.fd]
        real, calls = os.close, []

        def close(fd):
            calls.append(fd)
            real(fd)
            if len(calls) == 1:
                raise OSError(errno.EIO, "injected")

        with mock.patch.object(os, "close", close):
            install._release(run)
        self.assertEqual(sorted(calls), sorted(fds))
        for fd in fds:
            with self.assertRaises(OSError):
                os.fstat(fd)
        self.assertEqual((run.held, run.fd), ({}, None))

    def test_the_success_path_only_closes_the_descriptors(self):
        before = self.open_descriptors()
        report = self.init()
        self.assertEqual(self.open_descriptors(), before)
        self.assertFalse(report.failed, report.lines)
        self.assertEqual(sorted(os.listdir(self.target)), FILES)
        self.assertGreater((self.target / ".env").stat().st_size, 0)                           # nothing was emptied
        self.assertEqual(install.key_fingerprint_state(str(self.target)), "MATCH")
        self.assertEqual(os.listdir(self.parent), ["set"])
        self.assertFalse(any("STAGING_LEFT" in l or "LEFT_IN_PLACE" in l for l in report.lines), report.lines)

    def test_no_run_removes_a_name_at_any_step(self):
        """The audit events of the real removals (os.remove covers unlink, os.rmdir covers shutil.rmtree) never fire inside init_config."""
        events, armed = [], [False]

        def hook(event, args):
            if armed[0] and event in self.DELETIONS:
                events.append((event, [str(a) for a in args]))

        sys.addaudithook(hook)
        armed[0] = True
        try:
            for label, kw in self.failures().items():
                self.fresh_parent()
                self.init(**kw)
            self.fresh_parent()
            os.mkdir(self.target)                                # a refused run (the target exists) ends in the same cleanup
            self.init()
            self.fresh_parent()
            with mock.patch.object(install, "_rename_noreplace", side_effect=OSError(errno.EXDEV, "injected")):
                self.init()
        finally:
            armed[0] = False
        self.assertEqual(events, [])


@linux
class SecondInterruptTests(Base):
    """A second interrupt while the result is being determined: empty what is surely unpublished, leave alone what may be the published set."""

    def test_a_second_interrupt_in_the_cleanup_of_an_unpublished_run_still_empties_the_files(self):
        with mock.patch.object(install, "_rename_noreplace", side_effect=OSError(errno.EIO, "injected")), \
                mock.patch.object(install, "_discard", side_effect=KeyboardInterrupt):
            report = self.init()
        (staging,) = self.staging_dirs()
        self.assertEqual(report.unverified, ["INTERRUPTED_WHILE_FINISHING"], report.lines)
        self.assertIn(f"staging directory {staging} may remain (2 of its 2 files emptied)", "\n".join(report.lines))
        self.assertEqual([os.lstat(staging / n).st_size for n in FILES], [0, 0])
        self.assertNoSecretOnDisk()

    def test_a_second_interrupt_before_the_rename_ran_empties_the_files_too(self):
        with mock.patch.object(install, "_discard", side_effect=KeyboardInterrupt):
            report = self.init(fault=self.fail_at("record_written"))
        (staging,) = self.staging_dirs()
        self.assertEqual(report.unverified, ["INTERRUPTED_WHILE_FINISHING"], report.lines)
        self.assertEqual([os.lstat(staging / n).st_size for n in FILES], [0, 0])

    def test_a_second_interrupt_after_a_rename_that_may_have_worked_leaves_the_set_alone(self):
        real_state, calls = install._publication_state, []

        def state(*args):
            calls.append(1)
            if len(calls) > 1:                                   # the first call is the check before the rename
                raise KeyboardInterrupt
            return real_state(*args)

        with mock.patch.object(install, "_publication_state", state):
            report = self.init()
        self.assertEqual(report.unverified, ["INTERRUPTED_WHILE_FINISHING"], report.lines)
        self.assertGreater((self.target / ".env").stat().st_size, 0)                          # it was published: it is not touched
        self.assertGreater((self.target / "install-record.json").stat().st_size, 0)
        self.assertEqual(install.key_fingerprint_state(str(self.target)), "MATCH")
        self.assertNotIn("emptied", "\n".join(report.lines))


@linux
class TheLeftoverAsTheNextRunSeesItTests(Base):
    """The next run's preflight is unchanged: only a directory with no entry at all is STALE_STAGING_EMPTY; zero-length files are not."""

    def next_run(self):
        code, lines = fx.cli("preflight", self.sha, self.target, self.repo)
        self.assertEqual(code, 0, lines)
        return lines

    def test_an_emptied_leftover_is_reported_as_stale_staging_and_not_touched(self):
        self.init(fault=self.fail_at("record_written"))
        (staging,) = self.staging_dirs()
        before = fx.snapshot(staging), [(n, os.lstat(staging / n).st_ino) for n in sorted(os.listdir(staging))]
        lines = self.next_run()
        self.assertTrue(any(l.startswith("STALE_STAGING (not touched, may hold unpublished secrets") and staging.name in l for l in lines), lines)
        self.assertFalse(any(l.startswith("STALE_STAGING_EMPTY") for l in lines), lines)
        self.assertEqual((fx.snapshot(staging), [(n, os.lstat(staging / n).st_ino) for n in sorted(os.listdir(staging))]), before)
        self.assertEqual([os.lstat(staging / n).st_size for n in FILES], [0, 0])

    def test_a_directory_without_any_entry_is_still_stale_staging_empty(self):
        made = self.parent / ".set.staging-hand-made"
        made.mkdir(mode=0o700)
        lines = self.next_run()
        self.assertTrue(any(l.startswith("STALE_STAGING_EMPTY (not touched, holds nothing") and made.name in l for l in lines), lines)
        self.assertFalse(any(l.startswith("STALE_STAGING (") for l in lines), lines)

    def test_a_leftover_that_still_holds_content_is_reported_with_its_secrets_warning(self):
        self.init(fault=self.fail_at("record_written"))
        (staging,) = self.staging_dirs()
        (staging / "intruder.txt").write_text("not ours")
        lines = self.next_run()
        self.assertTrue(any(l.startswith("STALE_STAGING (") and staging.name in l for l in lines), lines)


if __name__ == "__main__":
    unittest.main(verbosity=2)
