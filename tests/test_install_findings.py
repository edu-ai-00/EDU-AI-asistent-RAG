"""B06-1 regression tests for the review findings R1-R4 (docs/plans/b06-implementation-review-20261005.md).

Run: python tests/test_install_findings.py   (standard library + git; synthetic repositories only, no Docker, no network)
R1 and R4 need Linux (modes, renameat2, signals): run them in WSL; elsewhere they are skipped.
"""
import errno
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import install_fixtures as fx  # noqa: E402
from install_fixtures import install  # noqa: E402

linux = unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux (renameat2, signals, modes)")
FILES = [".env", "install-record.json"]


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.repo, self.sha = fx.make_repo(self.tmp / "repo")
        self.parent = self.tmp / "cfg"
        self.parent.mkdir()
        if os.name == "posix":
            os.chmod(self.parent, 0o700)
        self.target = self.parent / "set"

    def init(self, **kw):
        return install.init_config(self.sha, str(self.target), str(self.repo), gen=fx.STUB_GEN, **kw)

    def noSecrets(self, report):
        for value in fx.STUB_VALUES.values():
            self.assertNotIn(value, "\n".join(report.lines))


@linux
class R1ReservedTargetTests(Base):
    """A foreign directory that appears at the target must never be replaced or removed."""

    def swap_target(self, then=None):
        """Fault hook: at the reservation (old code) or publication (fixed code) point, put a foreign empty directory at the target."""
        seen = {}

        def fault(step):
            if step in ("claimed", "before_publish"):
                if os.path.lexists(self.target):
                    os.rename(self.target, self.parent / "old-claim")
                os.mkdir(self.target, 0o755)
                seen["ino"] = os.lstat(self.target).st_ino
                if then:
                    raise then

        return fault, seen

    def assertForeignIntact(self, seen):
        self.assertTrue(os.path.lexists(self.target), "the foreign directory was removed by the cleanup")
        info = os.lstat(self.target)
        self.assertEqual(info.st_ino, seen["ino"], "the foreign directory was replaced")
        self.assertEqual(oct(info.st_mode & 0o7777), "0o755")
        self.assertEqual(os.listdir(self.target), [])
        self.assertEqual(fx.secret_staging(self.parent), [])

    def test_a_foreign_directory_that_replaces_the_reservation_is_not_overwritten_at_publication(self):
        fault, seen = self.swap_target()
        report = self.init(fault=fault)
        self.assertEqual(report.exit_code(), 2, report.lines)
        self.assertNotIn("CONFIG_PREPARED", report.lines)
        self.assertForeignIntact(seen)
        self.noSecrets(report)

    def test_a_foreign_directory_is_not_removed_by_the_cleanup_after_an_error(self):
        fault, seen = self.swap_target(then=OSError(errno.EIO, "injected"))
        report = self.init(fault=fault)
        self.assertEqual(report.exit_code(), 3, report.lines)
        self.assertForeignIntact(seen)
        self.noSecrets(report)

    def test_a_foreign_directory_is_not_removed_by_the_cleanup_after_an_interrupt(self):
        fault, seen = self.swap_target(then=KeyboardInterrupt())
        report = self.init(fault=fault)
        self.assertEqual(report.exit_code(), 3, report.lines)
        self.assertForeignIntact(seen)

    def test_every_kind_of_foreign_entry_at_the_target_is_refused_untouched(self):
        def empty_dir(p): os.mkdir(p, 0o755)
        def dir_with_file(p): os.mkdir(p, 0o755); (p / "keep").write_text("keep")
        def a_file(p): p.write_text("keep")
        def link(p): os.mkdir(self.parent / "real"); os.symlink(self.parent / "real", p)
        def dangling(p): os.symlink(self.parent / "missing", p)
        for make in (empty_dir, dir_with_file, a_file, link, dangling):
            for name in os.listdir(self.parent):
                path = self.parent / name
                shutil.rmtree(path) if path.is_dir() and not path.is_symlink() else path.unlink()

            def fault(step, make=make):
                if step in ("claimed", "before_publish"):
                    if os.path.lexists(self.target):
                        os.rename(self.target, self.parent / "old-claim")
                    make(self.target)

            report = self.init(fault=fault)
            with self.subTest(kind=make.__name__):
                self.assertEqual(report.exit_code(), 2, report.lines)
                self.assertTrue(os.path.lexists(self.target))
                self.assertEqual(fx.secret_staging(self.parent), [])

    def test_a_foreign_directory_that_replaces_the_staging_directory_is_not_cleaned_up(self):
        def fault(step):
            if step == "env_written":
                (staging,) = [n for n in os.listdir(self.parent) if ".staging-" in n]
                os.replace(self.parent / staging, self.parent / "moved-staging")
                os.mkdir(self.parent / staging, 0o755)
                self.foreign = self.parent / staging
                raise OSError(errno.EIO, "injected")

        report = self.init(fault=fault)
        self.assertEqual(report.exit_code(), 3, report.lines)
        self.assertTrue(self.foreign.is_dir(), "the foreign empty directory was removed by the cleanup")
        self.assertEqual(os.listdir(self.foreign), [])
        (line,) = [l for l in report.lines if l.startswith("STAGING_LEFT_EMPTIED: ")]
        self.assertIn(str(self.foreign), line)                                               # the full path of the name, and where the directory is now
        self.assertIn(str(self.parent / "moved-staging"), line)
        self.assertIn("1 of 1 files", line)
        self.assertEqual((self.parent / "moved-staging" / ".env").stat().st_size, 0)        # our file was emptied through its descriptor
        self.assertFalse(self.target.exists())
        self.noSecrets(report)

    def test_rename_noreplace_never_replaces_anything(self):
        for make in ("empty_dir", "dir_with_file", "file", "link", "dangling"):
            src, dst = self.parent / f"src_{make}", self.parent / f"dst_{make}"
            src.mkdir()
            (src / "ours").write_text("ours")
            if make == "empty_dir": dst.mkdir()
            elif make == "dir_with_file": dst.mkdir(); (dst / "keep").write_text("keep")
            elif make == "file": dst.write_text("keep")
            elif make == "link": os.symlink(src, dst)
            else: os.symlink(self.parent / "missing", dst)
            before = (os.lstat(src).st_ino, os.lstat(dst).st_ino if not os.path.islink(dst) else os.readlink(dst))
            with self.subTest(target=make), self.assertRaises(FileExistsError):
                install._rename_noreplace(str(src), str(dst))
            self.assertEqual((os.lstat(src).st_ino, os.lstat(dst).st_ino if not os.path.islink(dst) else os.readlink(dst)), before)
            self.assertEqual(os.listdir(src), ["ours"])
        free = self.parent / "free"
        install._rename_noreplace(str(self.parent / "src_file"), str(free))
        self.assertEqual(os.listdir(free), ["ours"])
        with self.assertRaises(FileNotFoundError):
            install._rename_noreplace(str(self.parent / "absent"), str(self.parent / "free2"))

    def test_a_filesystem_without_the_atomic_rename_is_unverified_not_a_fallback(self):
        for code in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            before = fx.snapshot(self.parent)
            with mock.patch.object(install, "_rename_noreplace", side_effect=OSError(code, "no")):
                report = self.init()
            with self.subTest(errno=code):
                self.assertEqual(report.unverified, ["PUBLISH_NOREPLACE_UNSUPPORTED"], report.lines)
                self.assertEqual(fx.snapshot(self.parent), before)


class R2PartialCloneTests(Base):
    """A promisor/partial clone is refused before any object is evaluated, and can never fetch."""

    def make_pair(self, marker):
        server, _ = fx.make_repo(self.tmp / ("server_" + marker))
        fx.git(server, "config", "uploadpack.allowFilter", "true")
        fx.git(server, "config", "uploadpack.allowAnySHA1InWant", "true")
        client = self.tmp / ("client_" + marker)
        client.mkdir()
        fx.git(client, "init", "-q")
        fx.git(client, "remote", "add", "origin", str(server))
        if marker == "extension":
            fx.git(client, "config", "core.repositoryformatversion", "1")
            fx.git(client, "config", "extensions.partialclone", "origin")
        else:
            fx.git(client, "config", "remote.origin.promisor", "true")
        (server / "extra.txt").write_text("x")
        fx.git(server, "add", "extra.txt")
        fx.git(server, "commit", "-q", "-m", "a commit the client never had")
        return server, client, fx.git(server, "rev-parse", "HEAD")

    @staticmethod
    def without_lazy_fetch_guard(real=subprocess.run):
        def run(cmd, *args, **kw):
            env = dict(kw.get("env") or os.environ)
            env.pop("GIT_NO_LAZY_FETCH", None)                  # a git that ignores the variable behaves as if it were absent
            kw["env"] = env
            return real(cmd, *args, **kw)
        return run

    def test_the_pair_really_lazy_fetches_when_nothing_prevents_it(self):
        for marker in ("flag", "extension"):
            _, client, missing = self.make_pair(marker)
            probe = self.tmp / ("probe_" + marker)
            shutil.copytree(client, probe)
            env = {k: v for k, v in os.environ.items() if k != "GIT_NO_LAZY_FETCH"}
            subprocess.run(["git", "-C", str(probe), "rev-parse", "--verify", "--quiet", missing + "^{commit}"], env=env, capture_output=True, check=False)
            kind = subprocess.run(["git", "-C", str(probe), "cat-file", "-t", missing], capture_output=True, text=True,
                                  env={**os.environ, "GIT_NO_LAZY_FETCH": "1"}).stdout.strip()
            self.assertEqual(kind, "commit", f"control failed for {marker}: this git does not lazy-fetch, the tests below prove nothing")

    def test_the_partial_clone_check_runs_before_any_object_is_evaluated(self):
        for marker in ("flag", "extension"):
            _, client, missing = self.make_pair(marker)
            calls = []
            real = install._git
            with mock.patch.object(install, "_git", lambda repo, *a: (calls.append(a), real(repo, *a))[1]):
                code, lines = fx.cli("preflight", missing, self.target, client)
            with self.subTest(marker=marker):
                self.assertEqual(code, 2, lines)
                self.assertTrue(any("PARTIAL_CLONE_UNSUPPORTED" in l for l in lines))
                self.assertTrue(calls and all(c[0] == "config" for c in calls), calls)

    def test_a_missing_commit_is_not_fetched_even_when_git_ignores_GIT_NO_LAZY_FETCH(self):
        for marker in ("flag", "extension"):
            server, client, missing = self.make_pair(marker)
            before = (fx.snapshot(client), fx.snapshot(server))
            with mock.patch.object(subprocess, "run", self.without_lazy_fetch_guard()):
                code, lines = fx.cli("preflight", missing, self.target, client)
            with self.subTest(marker=marker):
                self.assertEqual(code, 2, lines)
                self.assertTrue(any("PARTIAL_CLONE_UNSUPPORTED" in l for l in lines))
                self.assertEqual((fx.snapshot(client), fx.snapshot(server)), before)

    def test_even_if_the_check_missed_the_clone_no_transport_is_allowed(self):
        for marker in ("flag", "extension"):
            server, client, missing = self.make_pair(marker)
            def stable(path):                                   # git adds remote.*.partialclonefilter to .git/config even on a blocked attempt
                return {k: v for k, v in fx.snapshot(path).items() if k != os.path.join(".git", "config")}

            before = (stable(client), stable(server))
            with mock.patch.object(install, "_partial_clone_configured", return_value=False, create=True), \
                    mock.patch.object(subprocess, "run", self.without_lazy_fetch_guard()):
                code, lines = fx.cli("preflight", missing, self.target, client)
            with self.subTest(marker=marker):
                self.assertIn(code, (2, 3), lines)
                self.assertEqual((stable(client), stable(server)), before)       # no object, ref or file arrived

    def test_every_git_process_is_started_with_protocols_disabled(self):
        seen = []
        real = subprocess.run

        def spy(cmd, *args, **kw):
            seen.append(list(cmd))
            return real(cmd, *args, **kw)

        with mock.patch.object(subprocess, "run", spy):
            self.assertEqual(fx.cli("plan", self.sha, self.target, self.repo)[0], 0)
        self.assertTrue(seen)
        for cmd in seen:
            self.assertEqual(cmd[:2], ["git", "-C"])
            self.assertIn("protocol.allow=never", cmd)


class R3ReplaceObjectsTests(Base):
    """Replacement refs must never change what a pinned SHA reads."""

    def setUp(self):
        super().setUp()
        self.original_compose = fx.SYN_COMPOSE
        (self.repo / "docker-compose.yml").write_text(fx.SYN_COMPOSE.replace("SYN_SUBNET", "REPLACED_SUBNET"), encoding="utf-8")
        fx.git(self.repo, "commit", "-q", "-am", "B changes the compose variable")
        self.sha_b = fx.git(self.repo, "rev-parse", "HEAD")

    def read(self, sha=None):
        report = install.Report()
        sources = install.read_sources(str(self.repo), sha or self.sha, report)
        self.assertIsNotNone(sources, report.lines)
        return sources

    def test_a_replaced_commit_still_reads_its_own_files(self):
        fx.git(self.repo, "replace", self.sha, self.sha_b)
        sources = self.read()
        self.assertIn("SYN_SUBNET", sources.compose)
        self.assertNotIn("REPLACED_SUBNET", sources.compose)
        self.assertEqual(sources.compose_sha256, __import__("hashlib").sha256(self.original_compose.encode()).hexdigest())
        code, lines = fx.cli("plan", self.sha, self.target, self.repo)
        self.assertEqual(code, 0, lines)
        self.assertIn("SYN_SUBNET", fx.field(lines, "UNRESOLVED_REQUIRED"))

    def test_a_replaced_template_blob_still_reads_its_own_text(self):
        blob = fx.git(self.repo, "rev-parse", self.sha + ":.env.example")
        other = self.tmp / "other.txt"
        other.write_text(fx.SYN_TEMPLATE + "INJECTED_KEY=1\n", encoding="utf-8")
        fx.git(self.repo, "replace", blob, fx.git(self.repo, "hash-object", "-w", str(other)))
        sources = self.read()
        self.assertNotIn("INJECTED_KEY", sources.template)
        self.assertEqual(sources.template, fx.SYN_TEMPLATE)

    def test_an_inherited_environment_changes_nothing(self):
        fx.git(self.repo, "replace", self.sha, self.sha_b)
        for env in ({}, {"GIT_NO_REPLACE_OBJECTS": "0"}, {"GIT_NO_REPLACE_OBJECTS": ""}, {"GIT_REPLACE_REF_BASE": "refs/replace/"}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                self.assertNotIn("REPLACED_SUBNET", self.read().compose)

    def test_every_git_process_gets_the_variable_forced(self):
        seen = []
        real = subprocess.run

        def spy(cmd, *args, **kw):
            seen.append((list(cmd), kw.get("env") or {}))
            return real(cmd, *args, **kw)

        with mock.patch.dict(os.environ, {"GIT_NO_REPLACE_OBJECTS": "0"}), mock.patch.object(subprocess, "run", spy):
            self.assertEqual(fx.cli("plan", self.sha, self.target, self.repo)[0], 0)
        self.assertTrue(seen)
        for cmd, env in seen:
            self.assertEqual(env.get("GIT_NO_REPLACE_OBJECTS"), "1", cmd)


@linux
class R4SignalAfterPublicationTests(Base):
    """An interrupt right after the rename must be reported from the disk, not from a flag."""

    def real_rename(self):
        return install._rename_noreplace if hasattr(install, "_rename_noreplace") else os.rename

    def patch_rename(self, after):
        real = self.real_rename()

        def wrapper(src, dst, *rest):
            real(src, dst, *rest)
            after()

        if hasattr(install, "_rename_noreplace"):
            return mock.patch.object(install, "_rename_noreplace", wrapper)
        return mock.patch.object(install.os, "rename", wrapper)

    def assertPublished(self, report):
        self.assertEqual(sorted(os.listdir(self.target)), sorted(FILES))
        self.assertEqual(install.key_fingerprint_state(str(self.target)), "MATCH")
        self.assertFalse(any(l.startswith("UNVERIFIED INTERRUPTED") for l in report.lines), report.lines)
        self.assertFalse(any(l.startswith("LEFT_IN_PLACE") for l in report.lines), report.lines)
        self.assertEqual(report.exit_code(), 0, report.lines)
        self.assertIn("CONFIG_PREPARED", report.lines)
        self.assertEqual(fx.secret_staging(self.parent), [])
        self.noSecrets(report)

    def test_a_real_signal_delivered_right_after_the_rename(self):
        for signum in (signal.SIGTERM, signal.SIGINT):
            for name in os.listdir(self.parent):
                shutil.rmtree(self.parent / name)
            with self.subTest(signal=signum.name), self.patch_rename(lambda s=signum: os.kill(os.getpid(), s)):
                report = self.init()
                self.assertPublished(report)
                self.assertTrue(any(l.startswith("INTERRUPTED_AFTER_PUBLICATION") for l in report.lines), report.lines)

    def test_an_interrupt_or_error_that_escapes_the_rename_call_after_it_worked(self):
        for exc in (KeyboardInterrupt(), OSError(errno.EIO, "injected")):
            for name in os.listdir(self.parent):
                shutil.rmtree(self.parent / name)

            def boom(exc=exc):
                raise exc

            with self.subTest(exc=type(exc).__name__), self.patch_rename(boom):
                self.assertPublished(self.init())

    def test_an_unclear_state_is_unverified_and_nothing_is_removed(self):
        def hostile():
            os.replace(self.target, self.parent / "moved")       # our published set is moved away ...
            os.mkdir(self.target, 0o755)                         # ... and a foreign directory takes its place
            raise KeyboardInterrupt

        with self.patch_rename(hostile):
            report = self.init()
        self.assertEqual(report.exit_code(), 3, report.lines)
        self.assertEqual(report.unverified, ["PUBLICATION_STATE_UNKNOWN"])
        self.assertNotIn("CONFIG_PREPARED", report.lines)
        self.assertEqual(os.listdir(self.target), [])
        self.assertEqual(sorted(os.listdir(self.parent / "moved")), sorted(FILES))
        self.noSecrets(report)

    def test_after_an_interrupted_publication_the_next_run_agrees_with_the_disk(self):
        with self.patch_rename(lambda: os.kill(os.getpid(), signal.SIGTERM)):
            self.assertPublished(self.init())
        again = self.init()
        self.assertEqual(again.refused, ["TARGET_EXISTS_CONFIG_SET"])
        self.assertEqual(fx.field(again.lines, "EXISTING_KEY_FINGERPRINT"), "MATCH")


if __name__ == "__main__":
    unittest.main(verbosity=2)
