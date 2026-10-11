"""B06-1-R2 and B06-1-R3 regression tests: how install.py reads a pinned commit out of a local git repository.

R2: a partial/promisor repository is refused before any object is looked up, and no Git process can reach a remote.
R3: replacement refs (git replace) never change what a full SHA reads, nor what the evidence says about it.

Run: python tests/test_install_git_regression.py   (standard library + git; local synthetic repositories, no network)
The init-config evidence test needs Linux (renameat2) and is skipped elsewhere: run it in WSL or Linux.
"""
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import install_fixtures as fx  # noqa: E402
from install_fixtures import install  # noqa: E402

SCRIPT = Path(install.__file__)
linux = unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux (renameat2)")
COMPOSE, TEMPLATE = "docker-compose.yml", ".env.example"
# Inherited settings that would change how Git behaves; every test starts without them.
INHERITED = ("GIT_NO_LAZY_FETCH", "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_ALLOW_PROTOCOL", "GIT_CONFIG",
             "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_SYSTEM", "GIT_PROTOCOL_FROM_USER")
PROTOCOLS = ("file", "git", "ssh", "http", "https", "ftp", "ftps", "ext", "fd")
Pair = namedtuple("Pair", "client server sha missing trigger extra_env")


class GitSpy:
    """Records every git process install.py starts as (flags, verb, args, env passed), then runs it for real."""

    def __init__(self, strip=()):
        self.calls, self.strip, self.real = [], strip, subprocess.run

    def __enter__(self):
        self._patch = mock.patch.object(subprocess, "run", self._run)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()

    def _run(self, cmd, *args, **kw):
        assert cmd[:2] == ["git", "-C"], cmd
        i, flags = 3, []
        while cmd[i] == "-c":
            flags.append(cmd[i + 1])
            i += 2
        env = dict(kw.get("env") or os.environ)
        self.calls.append((flags, cmd[i], tuple(cmd[i + 1:]), dict(env)))
        for name in self.strip:
            env.pop(name, None)                         # a git that ignores the variable behaves as if it were absent
        return self.real(cmd, *args, **{**kw, "env": env})

    @property
    def verbs(self):
        return [verb for _, verb, _, _ in self.calls]


def put(repo, name, text):
    (Path(repo) / name).write_bytes(text.encode("utf-8"))


def pinned(repo, sha, path):
    """The bytes git itself reads for sha:path with replacement refs off: the oracle."""
    return subprocess.run(["git", "--no-replace-objects", "-C", str(repo), "cat-file", "blob", f"{sha}:{path}"],
                          capture_output=True, check=True).stdout


def sha256(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.empty_cfg = self.tmp / "empty.cfg"
        self.empty_cfg.write_text("")
        env = {k: v for k, v in os.environ.items()
               if k not in INHERITED and not k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}
        env.update(GIT_CONFIG_GLOBAL=str(self.empty_cfg), GIT_CONFIG_NOSYSTEM="1")
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.parent = self.tmp / "cfg"
        self.parent.mkdir()
        if install.IS_POSIX:
            os.chmod(self.parent, 0o700)
        self.target = self.parent / "set"

    @contextlib.contextmanager
    def env(self, **extra):
        """Inherited environment for the code under test; a None value removes the variable."""
        env = dict(os.environ)
        for key, value in extra.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        with mock.patch.dict(os.environ, env, clear=True):
            yield

    def hostile_cfg(self):
        path = self.tmp / "hostile.cfg"
        path.write_text('[protocol]\n\tallow = always\n[protocol "file"]\n\tallow = always\n')
        return str(path)

    def codes(self, lines):
        return [line.split()[1].rstrip(":") for line in lines if line.startswith(("REFUSED", "UNVERIFIED"))]

    def run_script(self, *argv, **env_extra):
        env = dict(os.environ)
        env.update({k: v for k, v in env_extra.items() if v is not None})
        done = subprocess.run([sys.executable, str(SCRIPT), *argv], capture_output=True, text=True, env=env, timeout=120)
        return done.returncode, done.stdout.splitlines()


# ── R2 ───────────────────────────────────────────────────────────────────────
class PromisorBase(Base):
    """Pairs of local repositories: a client that would lazy-fetch from a server next to it. No network is involved."""

    KINDS = ("flag", "extension", "include-file", "real-partial-clone")      # set in the repository, an include or a clone: refused
    ENV_KINDS = ("env-injected",)                                           # set only by the caller's environment: no git child sees it
    pairs = 0

    def make_server(self, tag):
        server, _ = fx.make_repo(self.tmp / ("server_" + tag))
        fx.git(server, "config", "uploadpack.allowFilter", "true")
        fx.git(server, "config", "uploadpack.allowAnySHA1InWant", "true")
        return server

    def make_pair(self, kind):
        self.pairs += 1
        tag = f"{kind}_{self.pairs}"
        server = self.make_server(tag)
        if kind == "real-partial-clone":
            client = self.tmp / ("client_" + tag)
            subprocess.run(["git", "clone", "-q", "--no-checkout", "--filter=blob:none", server.as_uri(), str(client)],
                           capture_output=True, text=True, check=True)
            sha = fx.git(server, "rev-parse", "HEAD")
            blob = fx.git(server, "rev-parse", f"{sha}:{COMPOSE}")
            return Pair(client, server, sha, blob, ["cat-file", "blob", f"{sha}:{COMPOSE}"], {})
        client = self.tmp / ("client_" + tag)
        client.mkdir()
        fx.git(client, "init", "-q")
        fx.git(client, "remote", "add", "origin", str(server))
        extra = {}
        if kind == "flag":
            fx.git(client, "config", "remote.origin.promisor", "true")
        elif kind == "extension":
            fx.git(client, "config", "core.repositoryformatversion", "1")
            fx.git(client, "config", "extensions.partialclone", "origin")
        elif kind == "include-file":
            included = self.tmp / f"included_{tag}.cfg"
            included.write_text('[remote "origin"]\n\tpromisor = true\n')
            fx.git(client, "config", "include.path", str(included))
        else:
            extra = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "remote.origin.promisor", "GIT_CONFIG_VALUE_0": "true"}
        put(server, "extra.txt", "x")
        fx.git(server, "add", "extra.txt")
        fx.git(server, "commit", "-q", "-m", "a commit the client never had")
        sha = fx.git(server, "rev-parse", "HEAD")
        return Pair(client, server, sha, sha, ["rev-parse", "--verify", "--quiet", sha + "^{commit}"], extra)

    def present(self, pair):
        """Is the object in the client? Asked without any chance of a lazy fetch."""
        done = subprocess.run(["git", "-C", str(pair.client), "cat-file", "-e", pair.missing], capture_output=True,
                              env={**os.environ, "GIT_NO_LAZY_FETCH": "1"})
        return done.returncode == 0

    def state(self, pair, skip_config=False):
        out = []
        for path in (pair.client, pair.server):
            snap = fx.snapshot(path)
            if skip_config:                             # git writes remote.*.partialclonefilter even on a blocked attempt
                snap.pop(os.path.join(".git", "config"), None)
            out.append(snap)
        return out


class R2PartialCloneTests(PromisorBase):
    def test_control_every_pair_lazy_fetches_when_nothing_prevents_it(self):
        for kind in self.KINDS + self.ENV_KINDS:
            pair = self.make_pair(kind)
            probe = Pair(self.tmp / ("probe_" + kind), *pair[1:])
            shutil.copytree(pair.client, probe.client)
            with self.subTest(kind=kind):
                self.assertFalse(self.present(probe), "the object must start out missing")
                with self.env(**pair.extra_env):
                    subprocess.run(["git", "-C", str(probe.client), *pair.trigger], capture_output=True, check=False)
                self.assertTrue(self.present(probe), "this git does not lazy-fetch here, so the tests below prove nothing")

    def test_a_partial_repository_is_refused_before_any_object_is_looked_up(self):
        for kind in self.KINDS:
            pair = self.make_pair(kind)
            before = (self.state(pair), fx.snapshot(self.parent))
            for cmd in ("preflight", "plan", "init-config"):
                with self.subTest(kind=kind, cmd=cmd), self.env(**pair.extra_env), GitSpy() as spy:
                    code, lines = fx.cli(cmd, pair.sha, self.target, pair.client)
                self.assertEqual((code, self.codes(lines)), (2, ["PARTIAL_CLONE_UNSUPPORTED"]), lines)
                self.assertTrue(spy.calls and set(spy.verbs) == {"config"}, spy.verbs)    # no rev-parse, no cat-file
                self.assertEqual((self.state(pair), fx.snapshot(self.parent)), before)    # the whole .git, config included
                self.assertFalse(self.target.exists())
                self.assertFalse(self.present(pair))

    def test_a_promisor_that_only_the_callers_environment_injects_reaches_no_git_child(self):
        """The injected setting is dropped from every child (it would compete with the protocol guard), so nothing can lazy-fetch."""
        for kind in self.ENV_KINDS:
            pair = self.make_pair(kind)
            before = (self.state(pair), fx.snapshot(self.parent))
            with self.env(**pair.extra_env), GitSpy(strip=("GIT_NO_LAZY_FETCH",)) as spy:
                code, lines = fx.cli("preflight", pair.sha, self.target, pair.client)
            self.assertEqual((code, self.codes(lines)), (2, ["SOURCE_SHA_NOT_A_LOCAL_COMMIT"]), lines)
            kept = {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"}
            self.assertTrue(spy.calls and not any(name.startswith("GIT_CONFIG") and name not in kept
                                                  for _, _, _, env in spy.calls for name in env), spy.calls)
            self.assertEqual((self.state(pair), fx.snapshot(self.parent)), before)
            self.assertFalse(self.present(pair))

    def test_a_repository_that_is_not_partial_is_checked_first_and_read_afterwards(self):
        repo, sha = fx.make_repo(self.tmp / "plain")
        with GitSpy() as spy:
            self.assertEqual(fx.cli("preflight", sha, self.target, repo)[0], 0)
        self.assertEqual([a for _, v, a, _ in spy.calls[:2]], [("--get", "extensions.partialclone"), ("--get-regexp", r"^remote\..*\.(promisor|partialclonefilter)$")])
        first_object = next(i for i, v in enumerate(spy.verbs) if v != "config")
        self.assertEqual(first_object, 2)
        self.assertEqual(spy.calls[first_object][2], ("--verify", "--quiet", sha + "^{commit}"))

    def test_the_refusal_does_not_depend_on_GIT_NO_LAZY_FETCH(self):
        for kind in self.KINDS:
            for label, inherited in (("absent", None), ("zero", "0")):
                pair = self.make_pair(kind)
                before = self.state(pair)
                with self.subTest(kind=kind, inherited=label), self.env(GIT_NO_LAZY_FETCH=inherited, **pair.extra_env),                         GitSpy(strip=("GIT_NO_LAZY_FETCH",)):
                    code, lines = fx.cli("preflight", pair.sha, self.target, pair.client)
                self.assertEqual((code, self.codes(lines)), (2, ["PARTIAL_CLONE_UNSUPPORTED"]), lines)
                self.assertEqual(self.state(pair), before)
                self.assertFalse(self.present(pair))

    def test_the_refusal_does_not_depend_on_the_inherited_protocol_settings(self):
        for label, extra in self.hostile().items():
            pair = self.make_pair("flag")
            before = self.state(pair)
            with self.subTest(hostile=label), self.env(**extra), GitSpy(strip=("GIT_NO_LAZY_FETCH",)):
                code, lines = fx.cli("preflight", pair.sha, self.target, pair.client)
            self.assertEqual((code, self.codes(lines)), (2, ["PARTIAL_CLONE_UNSUPPORTED"]), lines)
            self.assertEqual(self.state(pair), before)

    def test_an_unreadable_configuration_is_not_read_as_not_partial(self):
        pair = self.make_pair("flag")
        with open(pair.client / ".git" / "config", "a", encoding="utf-8", newline="\n") as handle:
            handle.write('[broken\n')
        before = self.state(pair)
        with GitSpy() as spy:
            code, lines = fx.cli("preflight", pair.sha, self.target, pair.client)
        self.assertEqual((code, self.codes(lines)), (3, ["GIT_FAILED"]), lines)
        self.assertEqual(set(spy.verbs), {"config"})
        self.assertEqual(self.state(pair), before)

    def hostile(self):
        """Inherited settings that beat protocol.allow=never, or that a mere -c protocol.allow=never cannot outvote."""
        cfg = self.hostile_cfg()
        return {
            "none": {},
            "global protocol.file.allow=always": {"GIT_CONFIG_GLOBAL": cfg},
            "system protocol.file.allow=always": {"GIT_CONFIG_SYSTEM": cfg, "GIT_CONFIG_NOSYSTEM": None},
            "GIT_ALLOW_PROTOCOL=file": {"GIT_ALLOW_PROTOCOL": "file"},
            "GIT_CONFIG_COUNT": {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "protocol.file.allow", "GIT_CONFIG_VALUE_0": "always"},
            "GIT_CONFIG_PARAMETERS": {"GIT_CONFIG_PARAMETERS": "'protocol.file.allow=always'"},
        }

    def test_control_a_bare_protocol_allow_never_does_not_stop_a_hostile_environment(self):
        for label, extra in self.hostile().items():
            if label == "none":
                continue
            pair = self.make_pair("flag")
            with self.subTest(hostile=label), self.env(**extra):
                subprocess.run(["git", "-C", str(pair.client), "-c", "protocol.allow=never", *pair.trigger], capture_output=True, check=False)
                self.assertTrue(self.present(pair), "the old flag alone stopped it here, so the next test does not prove the new one")

    def test_even_if_the_check_missed_the_clone_no_git_process_can_reach_the_remote(self):
        for kind in ("flag", "real-partial-clone"):
            for label, extra in self.hostile().items():
                pair = self.make_pair(kind)
                before = self.state(pair, skip_config=True)
                report = install.Report()
                with self.subTest(kind=kind, hostile=label), self.env(**extra), \
                        mock.patch.object(install, "_partial_clone_configured", return_value=False), \
                        GitSpy(strip=("GIT_NO_LAZY_FETCH",)):
                    sources = install.read_sources(str(pair.client), pair.sha, report)
                self.assertIsNone(sources, report.lines)
                self.assertEqual(report.exit_code(), 2, report.lines)
                self.assertEqual(self.state(pair, skip_config=True), before)         # no object, ref or pack arrived
                self.assertFalse(self.present(pair))

    def test_the_command_line_refuses_end_to_end_under_a_hostile_inherited_environment(self):
        pair = self.make_pair("real-partial-clone")
        before = (self.state(pair), fx.snapshot(self.parent))
        env = {"GIT_ALLOW_PROTOCOL": "file", "GIT_CONFIG_GLOBAL": self.hostile_cfg(), "GIT_NO_LAZY_FETCH": "0"}
        for cmd in ("preflight", "plan", "init-config"):
            with self.subTest(cmd=cmd):
                code, lines = self.run_script(cmd, "--source-sha", pair.sha, "--target-dir", str(self.target), "--repo", str(pair.client), **env)
                self.assertEqual((code, self.codes(lines)), (2, ["PARTIAL_CLONE_UNSUPPORTED"]), lines)
                self.assertEqual(lines[-2:], ["HOST_NOT_CHECKED", "INSTALLATION_NOT_STARTED"])
                self.assertEqual((self.state(pair), fx.snapshot(self.parent)), before)
                self.assertFalse(self.present(pair))


class EveryGitProcessTests(Base):
    """Whatever the inherited environment says, each git process is started the same locked-down way."""

    def test_every_git_process_denies_every_transport_and_ignores_inherited_overrides(self):
        repo, sha = fx.make_repo(self.tmp / "repo")
        hostile = {"GIT_NO_LAZY_FETCH": "0", "GIT_NO_REPLACE_OBJECTS": "0", "GIT_ALLOW_PROTOCOL": "file:git:ssh:http:https",
                   "GIT_DIR": str(self.tmp / "elsewhere"), "GIT_OBJECT_DIRECTORY": str(self.tmp / "elsewhere")}
        with self.env(**hostile), GitSpy() as spy:
            self.assertEqual(fx.cli("plan", sha, self.target, repo)[0], 0)
        self.assertGreaterEqual(len(spy.calls), 5)
        for flags, verb, args, env in spy.calls:
            with self.subTest(verb=verb, args=args):
                self.assertIn("protocol.allow=never", flags)
                for name in PROTOCOLS:
                    self.assertIn(f"protocol.{name}.allow=never", flags)
                self.assertEqual((env.get("GIT_NO_LAZY_FETCH"), env.get("GIT_NO_REPLACE_OBJECTS")), ("1", "1"))
                self.assertEqual({k for k in ("GIT_ALLOW_PROTOCOL", "GIT_DIR", "GIT_OBJECT_DIRECTORY") if k in env}, set())
                self.assertEqual(env.get("GIT_TERMINAL_PROMPT"), "0")


# ── R3 ───────────────────────────────────────────────────────────────────────
class R3ReplaceObjectsTests(Base):
    """A is the pinned commit; B is the commit its replacement refs point at, with different compose and template text."""

    B_COMPOSE = fx.SYN_COMPOSE.replace("SYN_SUBNET", "REPLACED_SUBNET")
    B_TEMPLATE = fx.SYN_TEMPLATE + "INJECTED_KEY=1\n"

    def setUp(self):
        super().setUp()
        self.repo, self.sha = fx.make_repo(self.tmp / "repo")
        put(self.repo, COMPOSE, self.B_COMPOSE)
        put(self.repo, TEMPLATE, self.B_TEMPLATE)
        fx.git(self.repo, "commit", "-q", "-am", "B: another compose file and another template")
        self.sha_b = fx.git(self.repo, "rev-parse", "HEAD")

    KINDS = ("commit", "tree", "compose-blob", "template-blob", "packed", "custom-base", "everything")

    def rev(self, expr):
        return fx.git(self.repo, "rev-parse", expr, env_extra={"GIT_NO_REPLACE_OBJECTS": "1"})    # ids are taken before any replacement

    def replace(self, kind):
        a, b = self.sha, self.sha_b
        ids = {name: (self.rev(f"{a}{spec}"), self.rev(f"{b}{spec}")) for name, spec in
               (("tree", "^{tree}"), ("compose", f":{COMPOSE}"), ("template", f":{TEMPLATE}"))}
        if kind in ("commit", "packed", "everything"):
            fx.git(self.repo, "replace", a, b)
        for name, kinds in (("tree", ("tree",)), ("compose", ("compose-blob", "everything")), ("template", ("template-blob", "everything"))):
            if kind in kinds:
                fx.git(self.repo, "replace", *ids[name])
        if kind == "packed":
            fx.git(self.repo, "pack-refs", "--all")
        if kind == "custom-base":
            fx.git(self.repo, "update-ref", f"refs/my-replace/{a}", b)

    def clear_replacements(self):
        refs = fx.git(self.repo, "for-each-ref", "--format=%(refname)", "refs/replace", "refs/my-replace")
        for ref in refs.splitlines():
            fx.git(self.repo, "update-ref", "-d", ref)

    def unprotected(self, path, **env):
        """What a plain git (nothing forced) reads for A:path, under the inherited env of a test."""
        with self.env(**env):
            return subprocess.run(["git", "-C", str(self.repo), "cat-file", "blob", f"{self.sha}:{path}"],
                                  capture_output=True, env=dict(os.environ)).stdout

    def read(self):
        report = install.Report()
        sources = install.read_sources(str(self.repo), self.sha, report)
        self.assertIsNotNone(sources, report.lines)
        return sources

    def base_env(self, kind):
        return {"GIT_REPLACE_REF_BASE": "refs/my-replace/"} if kind == "custom-base" else {}

    def test_the_oracle_is_the_original_text(self):
        self.assertEqual(pinned(self.repo, self.sha, COMPOSE).decode(), fx.SYN_COMPOSE)
        self.assertEqual(pinned(self.repo, self.sha, TEMPLATE).decode(), fx.SYN_TEMPLATE)
        self.assertNotEqual(pinned(self.repo, self.sha_b, COMPOSE), pinned(self.repo, self.sha, COMPOSE))

    def test_control_a_plain_git_does_read_the_replacement(self):
        for kind in self.KINDS:
            with self.subTest(kind=kind):
                self.replace(kind)
                changed = (self.unprotected(COMPOSE, **self.base_env(kind)).decode() != fx.SYN_COMPOSE
                           or self.unprotected(TEMPLATE, **self.base_env(kind)).decode() != fx.SYN_TEMPLATE)
                self.assertTrue(changed, "replacement had no effect on a plain git, so the tests below prove nothing")
            self.clear_replacements()

    def test_read_sources_returns_the_objects_of_the_given_sha(self):
        compose, template = pinned(self.repo, self.sha, COMPOSE), pinned(self.repo, self.sha, TEMPLATE)
        for kind in self.KINDS:
            with self.subTest(kind=kind):
                self.replace(kind)
                before = fx.snapshot(self.repo)
                with self.env(**self.base_env(kind)):
                    sources = self.read()
                self.assertEqual((sources.compose, sources.template), (fx.SYN_COMPOSE, fx.SYN_TEMPLATE))
                self.assertEqual((sources.compose_sha256, sources.template_sha256), (sha256(compose), sha256(template)))
                self.assertNotIn("REPLACED_SUBNET", sources.compose)
                self.assertNotIn("INJECTED_KEY", sources.template)
                self.assertEqual(fx.snapshot(self.repo), before)
            self.clear_replacements()

    def test_an_inherited_environment_cannot_switch_replacement_back_on(self):
        self.replace("commit")
        cfg = self.tmp / "use_replace.cfg"
        cfg.write_text("[core]\n\tuseReplaceRefs = true\n")
        variants = {
            "GIT_NO_REPLACE_OBJECTS=0": {"GIT_NO_REPLACE_OBJECTS": "0"},
            "GIT_NO_REPLACE_OBJECTS=empty": {"GIT_NO_REPLACE_OBJECTS": ""},
            "GIT_NO_REPLACE_OBJECTS=false": {"GIT_NO_REPLACE_OBJECTS": "false"},
            "GIT_NO_REPLACE_OBJECTS absent": {"GIT_NO_REPLACE_OBJECTS": None},
            "core.useReplaceRefs=true in global config": {"GIT_CONFIG_GLOBAL": str(cfg)},
            "core.useReplaceRefs=true in GIT_CONFIG_COUNT": {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.useReplaceRefs",
                                                              "GIT_CONFIG_VALUE_0": "true"},
        }
        for label, extra in variants.items():
            with self.subTest(env=label), self.env(**extra):
                sources = self.read()
                self.assertEqual(sources.compose, fx.SYN_COMPOSE)
                self.assertEqual(sources.template, fx.SYN_TEMPLATE)

    def test_the_command_line_reads_the_given_sha_under_an_inherited_environment(self):
        for kind in ("commit", "custom-base"):
            self.replace(kind)
            for label, inherited in (("absent", {}), ("zero", {"GIT_NO_REPLACE_OBJECTS": "0"})):
                with self.subTest(kind=kind, GIT_NO_REPLACE_OBJECTS=label):
                    code, lines = self.run_script("plan", "--source-sha", self.sha, "--target-dir", str(self.target),
                                                  "--repo", str(self.repo), **inherited, **self.base_env(kind))
                    self.assertEqual(code, 0, lines)
                    self.assertEqual(fx.field(lines, "SOURCE_SHA"), self.sha)
                    self.assertIn("SYN_SUBNET", fx.field(lines, "UNRESOLVED_REQUIRED"))
                    self.assertFalse(any("REPLACED_SUBNET" in l or "INJECTED_KEY" in l for l in lines), lines)
            self.clear_replacements()

    @linux
    def test_the_evidence_describes_the_objects_of_the_given_sha_and_no_others(self):
        self.replace("everything")
        compose, template = pinned(self.repo, self.sha, COMPOSE), pinned(self.repo, self.sha, TEMPLATE)
        for label, inherited in (("absent", None), ("zero", "0")):       # git reads any value of the variable as "off"; absent is the exposed case
            target = self.parent / ("set_" + label)
            with self.subTest(GIT_NO_REPLACE_OBJECTS=label), self.env(GIT_NO_REPLACE_OBJECTS=inherited):
                report = install.init_config(self.sha, str(target), str(self.repo), gen=fx.STUB_GEN)
                self.assertEqual(report.exit_code(), 0, report.lines)
                record = json.loads((target / "install-record.json").read_text(encoding="utf-8"))
                env_text = (target / ".env").read_text(encoding="utf-8")
                self.assertEqual(record["source_sha"], self.sha)
                self.assertEqual(record["sources"], {COMPOSE: sha256(compose), TEMPLATE: sha256(template)})
                self.assertNotEqual(record["sources"][COMPOSE], sha256(self.B_COMPOSE))
                self.assertNotEqual(record["sources"][TEMPLATE], sha256(self.B_TEMPLATE))
                self.assertEqual(env_text, install.render_env(fx.SYN_TEMPLATE, fx.STUB_VALUES))
                self.assertNotIn("INJECTED_KEY", env_text)
                self.assertEqual(record["env_sha256_at_creation"], sha256(env_text))
                self.assertIn("UNRESOLVED_REQUIRED:SYN_SUBNET", record["blockers"])
                self.assertFalse(any("REPLACED_SUBNET" in b for b in record["blockers"]), record["blockers"])


if __name__ == "__main__":
    unittest.main()
