"""B06-1, Codex final review, finding 2: an inherited GIT_CONFIG must not blind the partial-clone probe.

`git config` reads GIT_CONFIG as THE configuration file, so the probe saw an empty or foreign file, missed the repository's own
promisor setting and the object lookups after it could lazily fetch. The child environment is now built in one place (install._git_env).

Run: python tests/test_install_git_env.py   (standard library + git; local synthetic repositories, no network)
The remote-helper emulation needs a POSIX shell and is skipped elsewhere: run it in WSL or Linux.
"""
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import install_fixtures as fx  # noqa: E402
from install_fixtures import install  # noqa: E402

linux = unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux (a shell remote helper)")
DROPPED = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR",
           "GIT_ALLOW_PROTOCOL", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
           "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "GIT_CONFIG_KEY_1", "GIT_CONFIG_VALUE_1", "GIT_CONFIG_KEY_17", "GIT_CONFIG_VALUE_17")
KEPT = ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM")
FORCED = {"GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1", "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1"}


class Recorder:
    """Every git process install.py starts, as (verb, args, env as passed), run for real. `strip` removes variables from the real
    child only: a git older than 2.44 ignores GIT_NO_LAZY_FETCH, which is what this emulates."""

    def __init__(self, strip=()):
        self.calls, self.strip, self.real = [], strip, subprocess.run

    def __enter__(self):
        self._patch = mock.patch.object(subprocess, "run", self._run)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()

    def _run(self, cmd, *args, **kw):
        i = 3
        while cmd[i] == "-c":
            i += 2
        env = dict(kw.get("env") or os.environ)
        self.calls.append((cmd[i], tuple(cmd[i + 1:]), dict(env)))
        for name in self.strip:
            env.pop(name, None)
        return self.real(cmd, *args, **{**kw, "env": env})

    @property
    def verbs(self):
        return [verb for verb, _, _ in self.calls]

    def object_reads(self):
        """The calls that look an object up (and so could lazily fetch it): rev-parse --verify and cat-file."""
        return [(v, a) for v, a, _ in self.calls if v == "cat-file" or (v == "rev-parse" and a[:1] == ("--verify",))]


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.empty = self.tmp / "empty.cfg"
        self.empty.write_text("")
        self.missing = self.tmp / "missing.cfg"
        self.other = self.tmp / "other.cfg"
        self.other.write_text('[remote "x"]\n\tpromisor = false\n')
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_") or k.upper() == "GIT_EXEC_PATH"}
        env.update(GIT_CONFIG_GLOBAL=str(self.empty), GIT_CONFIG_NOSYSTEM="1")
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def inherited(self):
        """Inherited settings that each point `git config` away from the repository, or inject a value that contradicts it."""
        return {
            "GIT_CONFIG=empty file": {"GIT_CONFIG": str(self.empty)},
            "GIT_CONFIG=missing file": {"GIT_CONFIG": str(self.missing)},
            "GIT_CONFIG=file with remote.x.promisor=false": {"GIT_CONFIG": str(self.other)},
            "GIT_CONFIG_COUNT/KEY_0/VALUE_0 promisor=false": {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "remote.own.promisor",
                                                              "GIT_CONFIG_VALUE_0": "false"},
            "GIT_CONFIG_PARAMETERS promisor=false": {"GIT_CONFIG_PARAMETERS": "'remote.own.promisor=false'"},
            "all of them": {"GIT_CONFIG": str(self.empty), "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "remote.own.promisor",
                            "GIT_CONFIG_VALUE_0": "false", "GIT_CONFIG_PARAMETERS": "'remote.own.promisor=false'"},
        }

    def partial_repo(self, name="partial", **config):
        repo, sha = fx.make_repo(self.tmp / name)
        fx.git(repo, "config", "remote.own.promisor", "true")
        for key, value in config.items():
            fx.git(repo, "config", key.replace("__", "."), value)
        return repo, sha

    def read(self, repo, sha, **env):
        report = install.Report()
        with mock.patch.dict(os.environ, env), Recorder() as spy:
            sources = install.read_sources(str(repo), sha, report)
        return sources, report, spy


class EnvironmentBuilderTests(Base):
    """install._git_env: the one place that decides what a git child inherits (runs on every platform)."""

    def base(self):
        env = {name: "caller-" + name for name in DROPPED + KEPT}
        env.update(PATH="/synthetic/bin", HOME="/synthetic/home", LANG="C", SOMETHING_ELSE="kept", GIT_AUTHOR_NAME="keeps")
        env.update(GIT_TERMINAL_PROMPT="1", GIT_NO_LAZY_FETCH="0", GIT_OPTIONAL_LOCKS="1", GIT_NO_REPLACE_OBJECTS="0")
        return env

    def test_every_config_redirect_and_injection_is_dropped(self):
        env = install._git_env(self.base())
        for name in DROPPED:
            with self.subTest(name=name):
                self.assertNotIn(name, env)

    def test_the_variables_that_choose_which_global_and_system_file_everything_reads_stay(self):
        env = install._git_env(self.base())
        for name in KEPT:
            with self.subTest(name=name):
                self.assertEqual(env[name], "caller-" + name)

    def test_the_four_hardening_variables_are_forced_whatever_the_caller_has(self):
        self.assertEqual({k: v for k, v in install._git_env(self.base()).items() if k in FORCED}, FORCED)
        self.assertEqual({k: v for k, v in install._git_env({}).items() if k in FORCED}, FORCED)

    def test_nothing_else_of_the_callers_environment_is_lost_and_the_caller_is_not_changed(self):
        base = self.base()
        before = dict(base)
        env = install._git_env(base)
        self.assertEqual(base, before)
        wanted = {k: v for k, v in before.items() if k not in DROPPED and k not in FORCED}
        self.assertEqual({k: v for k, v in env.items() if k not in FORCED}, wanted)
        self.assertEqual(env["SOMETHING_ELSE"], "kept")

    def test_a_different_spelling_is_dropped_too(self):
        """Windows treats environment names case-insensitively, so git_config there is GIT_CONFIG."""
        env = install._git_env({"git_config": "x", "Git_Config_Count": "1", "git_config_key_0": "a", "GIT_CONFIG_VALUE_9": "b", "git_config_global": "g"})
        self.assertEqual(sorted(k.upper() for k in env if k.upper().startswith("GIT_CONFIG")), ["GIT_CONFIG_GLOBAL"])

    def test_the_default_base_is_the_process_environment(self):
        with mock.patch.dict(os.environ, {"GIT_CONFIG": str(self.empty), "GIT_CONFIG_COUNT": "1", "SOMETHING_ELSE": "kept"}):
            env = install._git_env()
        self.assertNotIn("GIT_CONFIG", env)
        self.assertNotIn("GIT_CONFIG_COUNT", env)
        self.assertEqual(env["SOMETHING_ELSE"], "kept")

    def test_every_git_process_is_started_with_that_environment(self):
        repo, sha = fx.make_repo(self.tmp / "plain")
        hostile = {"GIT_CONFIG": str(self.empty), "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "a.b", "GIT_CONFIG_VALUE_0": "c",
                   "GIT_CONFIG_PARAMETERS": "'a.b=c'", "GIT_NO_LAZY_FETCH": "0"}
        with mock.patch.dict(os.environ, hostile), Recorder() as spy:
            self.assertEqual(fx.cli("plan", sha, self.tmp / "set", repo)[0], 0)
        self.assertGreaterEqual(len(spy.calls), 5)
        self.assertIn("config", spy.verbs)
        self.assertIn("rev-parse", spy.verbs)
        for verb, args, env in spy.calls:
            with self.subTest(verb=verb, args=args):
                self.assertEqual({k for k in env if k.upper().startswith("GIT_CONFIG") and k not in KEPT}, set())
                self.assertEqual({k: v for k, v in env.items() if k in FORCED}, FORCED)


class InheritedConfigTests(Base):
    """A repository that sets remote.own.promisor=true is refused whatever GIT_CONFIG and friends the caller inherited."""

    def test_control_git_config_really_reads_only_the_inherited_file(self):
        """Without this the tests below prove nothing: a plain git must be blind to the repository's own setting under GIT_CONFIG."""
        repo, _ = self.partial_repo()
        probe = ("git", "-C", str(repo), "config", "--get-regexp", r"^remote\..*\.promisor$")
        self.assertEqual(subprocess.run(probe, capture_output=True).returncode, 0)
        for label, extra in self.inherited().items():
            if "GIT_CONFIG=" not in label and label != "all of them":
                continue
            with self.subTest(label=label):
                done = subprocess.run(probe, capture_output=True, env={**os.environ, **extra})
                self.assertNotIn(b"remote.own.promisor true", done.stdout.replace(b"\r", b""))

    def test_the_probe_still_sees_the_promisor_setting(self):
        repo, _ = self.partial_repo()
        self.assertTrue(install._partial_clone_configured(str(repo)))
        for label, extra in self.inherited().items():
            with self.subTest(label=label), mock.patch.dict(os.environ, extra):
                self.assertTrue(install._partial_clone_configured(str(repo)))

    def test_read_sources_refuses_and_starts_no_object_lookup(self):
        repo, sha = self.partial_repo()
        for label, extra in self.inherited().items():
            sources, report, spy = self.read(repo, sha, **extra)
            with self.subTest(label=label):
                self.assertIsNone(sources)
                self.assertEqual((report.refused, report.unverified), (["PARTIAL_CLONE_UNSUPPORTED"], []), report.lines)
                self.assertEqual(report.exit_code(), 2)
                self.assertTrue(spy.calls)
                self.assertEqual(set(spy.verbs), {"config"}, spy.verbs)
                self.assertEqual(spy.object_reads(), [])

    def test_the_extension_form_is_refused_too(self):
        repo, sha = fx.make_repo(self.tmp / "extension")
        fx.git(repo, "config", "core.repositoryformatversion", "1")
        fx.git(repo, "config", "extensions.partialclone", "origin")
        for label, extra in self.inherited().items():
            sources, report, spy = self.read(repo, sha, **extra)
            with self.subTest(label=label):
                self.assertIsNone(sources)
                self.assertEqual(report.refused, ["PARTIAL_CLONE_UNSUPPORTED"], report.lines)
                self.assertEqual(spy.object_reads(), [])

    def test_the_whole_effective_configuration_is_read_includes_and_global_scope_too(self):
        included = self.tmp / "included.cfg"
        included.write_text('[remote "own"]\n\tpromisor = true\n')
        via_include, sha_include = fx.make_repo(self.tmp / "via_include")
        fx.git(via_include, "config", "include.path", str(included))
        via_global, sha_global = fx.make_repo(self.tmp / "via_global")
        for label, repo, sha, extra in (("include", via_include, sha_include, {}),
                                        ("global", via_global, sha_global, {"GIT_CONFIG_GLOBAL": str(included)})):
            for name, inherited in self.inherited().items():
                sources, report, spy = self.read(repo, sha, **extra, **inherited)
                with self.subTest(scope=label, inherited=name):
                    self.assertIsNone(sources)
                    self.assertEqual(report.refused, ["PARTIAL_CLONE_UNSUPPORTED"], report.lines)
                    self.assertEqual(spy.object_reads(), [])

    def test_the_probe_runs_before_any_object_call_in_a_repository_that_is_not_partial(self):
        repo, sha = fx.make_repo(self.tmp / "plain")
        for label, extra in self.inherited().items():
            sources, report, spy = self.read(repo, sha, **extra)
            with self.subTest(label=label):
                self.assertIsNotNone(sources, report.lines)
                first = next(i for i, call in enumerate(spy.calls) if call[0] != "config")
                self.assertEqual(first, 2)
                self.assertEqual(spy.verbs[:first], ["config"] * first)
                self.assertEqual(spy.calls[first][:2], ("rev-parse", ("--verify", "--quiet", sha + "^{commit}")))

    def test_preflight_and_plan_start_no_object_lookup_before_the_probe_either(self):
        repo, sha = self.partial_repo()
        for cmd in ("preflight", "plan", "init-config"):
            for label, extra in self.inherited().items():
                with self.subTest(cmd=cmd, label=label), mock.patch.dict(os.environ, extra), Recorder() as spy:
                    code, lines = fx.cli(cmd, sha, self.tmp / "set", repo)
                self.assertEqual(code, 2, lines)
                self.assertTrue(any("PARTIAL_CLONE_UNSUPPORTED" in line for line in lines), lines)
                self.assertEqual(spy.object_reads(), [])
                self.assertEqual(set(spy.verbs), {"config"})

    def test_positive_control_a_normal_repository_is_still_read_under_an_inherited_config(self):
        repo, sha = fx.make_repo(self.tmp / "plain")
        for label, extra in self.inherited().items():
            sources, report, spy = self.read(repo, sha, **extra)
            with self.subTest(label=label):
                self.assertIsNotNone(sources, report.lines)
                self.assertFalse(report.failed, report.lines)
                self.assertEqual((sources.compose, sources.template), (fx.SYN_COMPOSE, fx.SYN_TEMPLATE))
                self.assertEqual(sources.compose_sha256, install.sha256_hex(fx.SYN_COMPOSE))

    def test_the_command_line_reads_a_normal_repository_under_an_inherited_config(self):
        repo, sha = fx.make_repo(self.tmp / "plain")
        script = Path(install.__file__)
        for label, extra in self.inherited().items():
            done = subprocess.run([sys.executable, str(script), "plan", "--source-sha", sha, "--target-dir", str(self.tmp / "set"),
                                   "--repo", str(repo)], capture_output=True, text=True, env={**os.environ, **extra}, timeout=120)
            with self.subTest(label=label):
                self.assertEqual(done.returncode, 0, done.stdout)
                self.assertEqual(fx.field(done.stdout.splitlines(), "SOURCE_SHA"), sha)


@linux
class OldGitEmulationTests(Base):
    """What Codex's probe showed: git before 2.44 ignores GIT_NO_LAZY_FETCH, and a remote helper named by protocol.foo.allow=always in the
    repository's own configuration is not among the protocols install.py denies. The helper must never run, whatever GIT_CONFIG says."""

    def setUp(self):
        super().setUp()
        self.repo, self.sha = self.partial_repo("lazy")
        fx.git(self.repo, "config", "remote.own.url", "foo::synthetic-no-network")
        fx.git(self.repo, "config", "protocol.foo.allow", "always")
        self.marker = self.tmp / "helper-called"
        helpers = self.tmp / "helpers"
        helpers.mkdir()
        helper = helpers / "git-remote-foo"
        helper.write_bytes(b'#!/bin/sh\nprintf called > "' + self.marker.as_posix().encode() + b'"\nexit 1\n')
        helper.chmod(helper.stat().st_mode | stat.S_IXUSR)
        loose = self.repo / ".git" / "objects" / self.sha[:2] / self.sha[2:]
        loose.chmod(0o600)
        loose.unlink()                                       # the commit is missing: looking it up is what would fetch it
        os.environ["PATH"] = str(helpers) + os.pathsep + os.environ["PATH"]

    def test_control_without_the_fix_a_plain_lookup_does_run_the_helper(self):
        env = {k: v for k, v in os.environ.items() if k != "GIT_NO_LAZY_FETCH"}
        subprocess.run(["git", "-C", str(self.repo), "rev-parse", "--verify", "--quiet", self.sha + "^{commit}"], env=env, capture_output=True)
        self.assertTrue(self.marker.exists(), "the helper did not run, so this emulation proves nothing")

    def test_the_helper_is_never_executed_with_an_inherited_config(self):
        for label, extra in self.inherited().items():
            sources, report, spy = None, install.Report(), None
            with mock.patch.dict(os.environ, extra), Recorder(strip=("GIT_NO_LAZY_FETCH",)) as spy:
                sources = install.read_sources(str(self.repo), self.sha, report)
            with self.subTest(label=label):
                self.assertFalse(self.marker.exists(), "the remote helper was executed")
                self.assertIsNone(sources)
                self.assertEqual(report.refused, ["PARTIAL_CLONE_UNSUPPORTED"], report.lines)
                self.assertEqual(spy.object_reads(), [])

    def test_the_helper_is_never_executed_through_the_command_line_either(self):
        env = {**os.environ, "GIT_CONFIG": str(self.empty)}
        for cmd in ("preflight", "plan", "init-config"):
            done = subprocess.run([sys.executable, str(Path(install.__file__)), cmd, "--source-sha", self.sha,
                                   "--target-dir", str(self.tmp / "set"), "--repo", str(self.repo)],
                                  capture_output=True, text=True, env=env, timeout=120)
            with self.subTest(cmd=cmd):
                self.assertEqual(done.returncode, 2, done.stdout)
                self.assertIn("REFUSED PARTIAL_CLONE_UNSUPPORTED", done.stdout)
                self.assertFalse(self.marker.exists(), "the remote helper was executed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
