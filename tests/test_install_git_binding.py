"""Independent review B of B06-1 (R2/R3 and the one-time secrets overview): regressions for what the review found.

R2-b1  remote.X.partialclonefilter alone, or a whitespace-only extensions.partialclone name, is a promisor remote to git, but was not
       refused before the object lookup (on Git < 2.44, where GIT_NO_LAZY_FETCH does not exist, a remote helper of a user-allowed
       protocol (protocol.foo.allow=always) was started).
R3-b2  the content is bound to the full SHA only by trusting git: a symlink in the tree was read as "the file", and an object whose
       content does not hash to its name (forged loose object, replacement that git applies anyway, alternate) was read as the commit's.
K7-b3  emit() failing (dead pipe, encoding) ended in a traceback, exit 1 and no markers although the set was already published.

Run: python tests/test_install_git_binding.py   (standard library + git; local synthetic repositories, no network)
"""
import io
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import install_fixtures as fx  # noqa: E402
from install_fixtures import install  # noqa: E402

linux = unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux (sh canary, renameat2)")
COMPOSE, TEMPLATE = "docker-compose.yml", ".env.example"


def codes(report):
    return [line.split()[1].rstrip(":") for line in report.lines if line.startswith(("REFUSED", "UNVERIFIED"))]


def plain_git(repo, *args, env=None):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, env={**os.environ, **(env or {})})


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        empty = self.tmp / "empty.cfg"
        empty.write_text("")
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") or k in ("GIT_EXEC_PATH",)}
        env.update(GIT_CONFIG_GLOBAL=str(empty), GIT_CONFIG_NOSYSTEM="1")
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.parent = self.tmp / "cfg"
        self.parent.mkdir()
        if install.IS_POSIX:
            os.chmod(self.parent, 0o700)
        self.target = self.parent / "set"


class PartialCloneFilterTests(Base):
    """Pairs of local repositories (client lacks a commit the server has); no network."""

    n = 0

    def pair(self, kind):
        PartialCloneFilterTests.n += 1
        tag = f"{kind}{self.n}"
        server, _ = fx.make_repo(self.tmp / f"server_{tag}")
        fx.git(server, "config", "uploadpack.allowFilter", "true")
        fx.git(server, "config", "uploadpack.allowAnySHA1InWant", "true")
        client = self.tmp / f"client_{tag}"
        client.mkdir()
        fx.git(client, "init", "-q")
        remote = str(server).replace("\\", "/")
        if kind == "filter-only":
            fx.git(client, "remote", "add", "origin", remote)
            fx.git(client, "config", "remote.origin.partialclonefilter", "blob:none")
        elif kind == "blank-extension":
            fx.git(client, "config", "core.repositoryformatversion", "1")
            with open(client / ".git" / "config", "a", encoding="utf-8", newline="\n") as h:
                h.write(f'[extensions]\n\tpartialclone = " "\n[remote " "]\n\turl = {remote}\n')
        (server / "x.txt").write_text("x")
        fx.git(server, "add", "x.txt")
        fx.git(server, "commit", "-q", "-m", "x")
        return client, fx.git(server, "rev-parse", "HEAD")

    def present(self, client, sha):
        return plain_git(client, "cat-file", "-e", sha, env={"GIT_NO_LAZY_FETCH": "1"}).returncode == 0

    def test_control_plain_git_lazy_fetches_for_these_configurations(self):
        for kind in ("filter-only", "blank-extension"):
            client, sha = self.pair(kind)
            with self.subTest(kind=kind):
                plain_git(client, "rev-parse", "--verify", "--quiet", sha + "^{commit}")
                self.assertTrue(self.present(client, sha), "this git does not treat the configuration as a promisor; the tests below prove nothing")

    def test_they_are_refused_before_any_object_is_looked_up(self):
        for kind in ("filter-only", "blank-extension"):
            client, sha = self.pair(kind)
            before = fx.snapshot(client)
            verbs = []
            real = subprocess.run
            with self.subTest(kind=kind), mock.patch.object(
                    subprocess, "run", lambda cmd, *a, **kw: (verbs.append(next(c for c in cmd[3:] if c != "-c" and "=" not in c)), real(cmd, *a, **kw))[1]):
                code, lines = fx.cli("preflight", sha, self.target, client)
            self.assertEqual((code, [l.split()[1].rstrip(":") for l in lines if l.startswith("REFUSED")]), (2, ["PARTIAL_CLONE_UNSUPPORTED"]), lines)
            self.assertEqual(set(verbs), {"config"}, verbs)
            self.assertEqual(fx.snapshot(client), before)
            self.assertFalse(self.present(client, sha))

    @linux
    def test_a_user_allowed_remote_helper_is_never_started_even_when_git_ignores_GIT_NO_LAZY_FETCH(self):
        """The K9 gap (protocol.foo.allow=always) was reachable through remote.X.partialclonefilter on Git < 2.44 (Ubuntu 22.04/24.04)."""
        client, sha = self.pair("filter-only")
        marker = self.tmp / "helper_ran"
        bindir = self.tmp / "bin"
        bindir.mkdir()
        helper = bindir / "git-remote-foo"
        helper.write_text(f"#!/bin/sh\necho ran >> {marker}\nexit 1\n")
        helper.chmod(0o755)
        fx.git(client, "remote", "set-url", "origin", "foo::anything")
        fx.git(client, "config", "protocol.foo.allow", "always")
        real = subprocess.run

        def old_git(cmd, *a, **kw):                      # Git < 2.44 has no GIT_NO_LAZY_FETCH
            env = dict(kw.get("env") or os.environ)
            env.pop("GIT_NO_LAZY_FETCH", None)
            env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
            return real(cmd, *a, **{**kw, "env": env})

        with mock.patch.object(subprocess, "run", old_git):
            code, lines = fx.cli("preflight", sha, self.target, client)
            self.assertEqual(code, 2, lines)
            self.assertFalse(marker.exists(), "a remote helper was started by a refused run")
            with mock.patch.object(install, "_partial_clone_configured", return_value=False):          # control: the helper does run when the check is blind
                fx.cli("preflight", sha, self.target, client)
            self.assertTrue(marker.exists(), "the control did not start the helper, so the assertion above proves nothing")


    @linux
    def test_the_builtin_fd_transport_is_denied_too(self):
        """git ships transports for ext, fd, ftp(s) and http(s); fd was the one the second layer did not name: with the first layer blind
        and Git < 2.44, `fd::0` (+ protocol.fd.allow=always) started a helper that waits for a peer on an inherited descriptor."""
        client, sha = self.pair("filter-only")
        fx.git(client, "remote", "set-url", "origin", "fd::0")
        fx.git(client, "config", "protocol.fd.allow", "always")
        real = subprocess.run

        def old_git(cmd, *a, **kw):
            env = dict(kw.get("env") or os.environ)
            env.pop("GIT_NO_LAZY_FETCH", None)
            return real(cmd, *a, **{**kw, "env": env, "timeout": 4})

        without_fd = ("-c", "protocol.allow=never", *(a for p in ("file", "git", "ssh", "http", "https", "ftp", "ftps", "ext")
                                                       for a in ("-c", f"protocol.{p}.allow=never")))
        with mock.patch.object(install, "_partial_clone_configured", return_value=False), mock.patch.object(subprocess, "run", old_git):
            with mock.patch.object(install, "_NO_TRANSPORT", without_fd):          # control: without the entry the helper starts and waits
                _, lines = fx.cli("preflight", sha, self.target, client)
            self.assertTrue(any("GIT_UNAVAILABLE" in l for l in lines), lines)
            code, lines = fx.cli("preflight", sha, self.target, client)
        self.assertEqual(code, 2, lines)
        self.assertTrue(any("SOURCE_SHA_NOT_A_LOCAL_COMMIT" in l for l in lines), lines)


class BindingTests(Base):
    def make(self):
        repo, sha = fx.make_repo(self.tmp / "repo")
        return repo, sha

    def read(self, repo, sha):
        report = install.Report()
        return install.read_sources(str(repo), sha, report), report

    def test_a_symlink_in_place_of_the_compose_file_is_refused(self):
        repo, _ = self.make()
        blob = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"], input=b"docs/other-compose.yml",
                              capture_output=True, check=True).stdout.decode().strip()
        fx.git(repo, "update-index", "--add", "--cacheinfo", f"120000,{blob},{COMPOSE}")
        fx.git(repo, "commit", "-q", "-m", "compose is a symlink")
        sources, report = self.read(repo, fx.git(repo, "rev-parse", "HEAD"))
        self.assertIsNone(sources)
        self.assertEqual((report.exit_code(), codes(report)), (2, ["SOURCE_FILE_NOT_A_REGULAR_FILE"]))

    def test_an_executable_regular_file_is_still_read(self):
        repo, _ = self.make()
        blob = fx.git(repo, "rev-parse", f"HEAD:{COMPOSE}")
        fx.git(repo, "update-index", "--cacheinfo", f"100755,{blob},{COMPOSE}")
        fx.git(repo, "commit", "-q", "-m", "x")
        sources, report = self.read(repo, fx.git(repo, "rev-parse", "HEAD"))
        self.assertIsNotNone(sources, report.lines)

    def forge(self, objects_dir, oid, kind, content):
        loose = Path(objects_dir) / oid[:2] / oid[2:]
        loose.parent.mkdir(parents=True, exist_ok=True)
        if loose.exists():
            os.chmod(loose, stat.S_IWRITE | stat.S_IREAD)
        loose.write_bytes(zlib.compress(b"%s %d\0" % (kind.encode(), len(content)) + content))

    def test_control_plain_git_reads_a_forged_loose_object_as_the_real_one(self):
        repo, sha = self.make()
        blob = fx.git(repo, "rev-parse", f"{sha}:{COMPOSE}")
        self.forge(repo / ".git" / "objects", blob, "blob", b"name: forged\n")
        self.assertEqual(plain_git(repo, "cat-file", "blob", f"{sha}:{COMPOSE}").stdout, b"name: forged\n")

    def test_an_object_that_does_not_hash_to_its_name_is_not_read(self):
        for kind, spec in (("blob", f"{{sha}}:{COMPOSE}"), ("blob", f"{{sha}}:{TEMPLATE}"), ("tree", "{sha}^{{tree}}")):
            repo, sha = fx.make_repo(self.tmp / f"repo_{kind}_{spec[-4:]}")
            oid = fx.git(repo, "rev-parse", spec.format(sha=sha))
            real = subprocess.run(["git", "-C", str(repo), "cat-file", kind, oid], capture_output=True, check=True).stdout
            self.forge(repo / ".git" / "objects", oid, kind, real + (b"\n" if kind == "blob" else b"100644 z\0" + bytes(20)))   # a tree gets one more entry
            with self.subTest(kind=kind, spec=spec):
                sources, report = self.read(repo, sha)
                self.assertIsNone(sources)
                self.assertEqual((report.exit_code(), codes(report)), (3, ["SOURCE_OBJECTS_FAILED_VERIFICATION"]))

    def test_an_object_forged_in_an_alternate_is_not_read(self):
        donor, sha = fx.make_repo(self.tmp / "donor")
        holder = self.tmp / "holder"
        holder.mkdir()
        fx.git(holder, "init", "-q")
        (holder / ".git" / "objects" / "info").mkdir(parents=True, exist_ok=True)
        with open(holder / ".git" / "objects" / "info" / "alternates", "w", encoding="utf-8", newline="\n") as handle:      # LF, not CRLF
            handle.write(str(donor / ".git" / "objects").replace("\\", "/") + "\n")
        sources, report = self.read(holder, sha)
        self.assertIsNotNone(sources, report.lines)                    # a genuine alternate is fine
        blob = fx.git(donor, "rev-parse", f"{sha}:{COMPOSE}")
        self.forge(donor / ".git" / "objects", blob, "blob", b"name: forged\n")
        sources, report = self.read(holder, sha)
        self.assertIsNone(sources)
        self.assertEqual(codes(report), ["SOURCE_OBJECTS_FAILED_VERIFICATION"])

    def test_a_replacement_is_caught_even_when_git_would_apply_it_anyway(self):
        """Defence in depth: with GIT_NO_REPLACE_OBJECTS removed from the child (a git that ignores it), the hash check still refuses."""
        repo, a = self.make()
        (repo / COMPOSE).write_text(fx.SYN_COMPOSE.replace("SYN_SUBNET", "B_ONLY_SUBNET"))
        fx.git(repo, "commit", "-q", "-am", "B")
        b = fx.git(repo, "rev-parse", "HEAD")
        fx.git(repo, "replace", a, b)
        real = subprocess.run

        def ignoring(cmd, *args, **kw):
            env = dict(kw.get("env") or os.environ)
            env.pop("GIT_NO_REPLACE_OBJECTS", None)
            return real(cmd, *args, **{**kw, "env": env})

        with mock.patch.object(subprocess, "run", ignoring):
            sources, report = self.read(repo, a)
        self.assertIsNone(sources)
        self.assertTrue(codes(report) and codes(report)[0] in ("SOURCE_OBJECTS_FAILED_VERIFICATION", "SOURCE_SHA_NOT_A_LOCAL_COMMIT"), report.lines)


class EmitFailureTests(unittest.TestCase):
    def test_a_dead_stdout_does_not_change_the_exit_status_or_raise(self):
        class Dead(io.StringIO):
            def write(self, s):
                raise BrokenPipeError(32, "Broken pipe")

            def flush(self):
                raise BrokenPipeError(32, "Broken pipe")

        with tempfile.TemporaryDirectory() as td:
            repo, sha = fx.make_repo(Path(td) / "repo")
            parent = Path(td) / "cfg"
            parent.mkdir()
            with mock.patch.object(sys, "stdout", Dead()):
                code = install.main(["plan", "--source-sha", sha, "--target-dir", str(parent / "set"), "--repo", str(repo)])
        self.assertEqual(code, 0)

    @linux
    def test_a_non_ascii_path_under_an_ascii_stdout_still_ends_with_the_markers(self):
        with tempfile.TemporaryDirectory() as td:
            repo, sha = fx.make_repo(Path(td) / "repo")
            parent = Path(td) / "cfg"
            parent.mkdir(mode=0o700)
            done = subprocess.run([sys.executable, str(Path(install.__file__)), "plan", "--source-sha", sha, "--target-dir",
                                   str(parent / "čfg"), "--repo", str(repo)], capture_output=True, text=True,
                                  env={**os.environ, "PYTHONIOENCODING": "ascii"})
        self.assertEqual(done.returncode, 0, done.stderr[-300:])
        self.assertEqual(done.stdout.splitlines()[-2:], ["HOST_NOT_CHECKED", "INSTALLATION_NOT_STARTED"])


if __name__ == "__main__":
    unittest.main()
