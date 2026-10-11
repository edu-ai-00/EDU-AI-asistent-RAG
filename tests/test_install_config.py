"""B06-1 install.py init-config: one protected configuration set, published whole or not at all.

Run: python tests/test_install_config.py   (Linux; standard library + git; synthetic data and temporary directories only)
Writing tests are skipped elsewhere (modes, renameat2): run them in WSL or Linux.
"""
import errno
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from hashlib import sha256
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import install_fixtures as fx  # noqa: E402
from install_fixtures import install  # noqa: E402

import envfile  # noqa: E402

CHILD = Path(__file__).resolve().parent / "install_child.py"
FILES = [".env", "install-record.json"]
posix = unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux (modes, renameat2)")


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.repo, self.sha = fx.make_repo(self.tmp / "repo")
        self.parent = self.tmp / "cfg"
        self.parent.mkdir()
        if install.IS_POSIX:
            os.chmod(self.parent, 0o700)
        self.target = self.parent / "set"

    def init(self, target=None, **kw):
        return install.init_config(self.sha, str(target or self.target), str(self.repo), **kw)

    def text(self, report):
        return "\n".join(report.lines)

    def assertNoSecrets(self, text):
        for value in fx.STUB_VALUES.values():
            self.assertNotIn(value, text)

    def listing(self):
        return sorted(n for n in os.listdir(self.parent) if not fx.is_emptied_staging(self.parent / n))      # an emptied staging is announced, not removed

    def codes(self, report):
        return report.refused + report.unverified

    def env_values(self, target=None):
        return envfile.read_env((target or self.target) / ".env")


@posix
class PublishTests(Base):
    def test_success_creates_exactly_the_protected_set(self):
        report = self.init(gen=fx.STUB_GEN)
        self.assertFalse(report.failed, report.lines)
        self.assertEqual(self.listing(), ["set"])                       # no staging left behind
        self.assertEqual(sorted(os.listdir(self.target)), sorted(FILES))
        self.assertEqual(oct(os.stat(self.target).st_mode & 0o7777), "0o700")
        for name in FILES:
            self.assertEqual(oct(os.stat(self.target / name).st_mode & 0o7777), "0o600")
            self.assertNotIn(b"\r", (self.target / name).read_bytes())
        self.assertIn("CONFIG_PREPARED", report.lines)
        self.assertEqual(fx.field(report.lines, "CONFIG_FILE"), str(self.target / ".env"))

    def test_env_is_the_template_with_only_the_four_secret_lines_replaced(self):
        self.init(gen=fx.STUB_GEN)
        expected = []
        for line in fx.SYN_TEMPLATE.split("\n"):
            key = line.split("=", 1)[0]
            expected.append(f"{key}={fx.STUB_VALUES[key]}" if key in fx.STUB_VALUES and "=" in line else line)
        self.assertEqual((self.target / ".env").read_text(), "\n".join(expected))
        values = self.env_values()
        for key in ("LANGFLOW_API_KEY", "OPENAI_API_KEY"):
            self.assertTrue(envfile.is_placeholder(values[key]))        # not generated, not invented
        self.assertNotIn("SYN_SUBNET", values)                          # a required input is named, never made up
        self.assertEqual(values["SYN_CONST"], "literal")

    def test_record_fingerprints_are_full_sha256_and_it_is_not_a_ready_marker(self):
        self.init(gen=fx.STUB_GEN, clock=lambda: __import__("datetime").datetime(2026, 10, 5, 12, 0, 0, tzinfo=__import__("datetime").timezone.utc))
        record = json.loads((self.target / "install-record.json").read_text())
        key = fx.STUB_VALUES["LANGFLOW_SECRET_KEY"]
        self.assertEqual(record["langflow_secret_key_sha256"], sha256(key.encode()).hexdigest())
        self.assertEqual(len(record["langflow_secret_key_sha256"]), 64)
        self.assertEqual(record["env_sha256_at_creation"], sha256((self.target / ".env").read_bytes()).hexdigest())
        self.assertEqual(record["sources"], {"docker-compose.yml": sha256(fx.SYN_COMPOSE.encode()).hexdigest(),
                                             ".env.example": sha256(fx.SYN_TEMPLATE.encode()).hexdigest()})
        self.assertEqual((record["schema"], record["status"], record["source_sha"], record["created_utc"]),
                         (install.RECORD_SCHEMA, "CONFIG_PREPARED", self.sha, "2026-10-05T12:00:00Z"))
        self.assertEqual((record["host_checked"], record["installation_started"], record["ready_for_install"]), (False, False, False))
        self.assertEqual(record["blockers"], ["HOST_NOT_CHECKED", "MISSING_MANUAL:OPENAI_API_KEY", "UNRESOLVED_REQUIRED:SYN_SUBNET"])
        self.assertEqual(record["input_gaps"], {"missing_manual": ["OPENAI_API_KEY"], "service_issued_later": ["LANGFLOW_API_KEY"],
                                                "unresolved_required": ["SYN_SUBNET"]})
        self.assertEqual(record["generated_keys"], list(install.GENERATED))
        self.assertNotIn(key, (self.target / "install-record.json").read_text())

    def test_cli_output_names_gaps_and_never_a_secret(self):
        code, lines = fx.cli("init-config", self.sha, self.target, self.repo)
        self.assertEqual(code, 0, lines)
        out = "\n".join(lines)
        self.assertEqual(lines[-2:], ["HOST_NOT_CHECKED", "INSTALLATION_NOT_STARTED"])
        self.assertIn("CONFIG_PREPARED", lines)
        self.assertEqual(fx.field(lines, "MISSING_MANUAL"), "OPENAI_API_KEY")
        self.assertEqual(fx.field(lines, "UNRESOLVED_REQUIRED"), "SYN_SUBNET")
        values = self.env_values()
        for key in install.GENERATED:
            self.assertNotIn(values[key], out)
        self.assertEqual(fx.field(lines, "LANGFLOW_SECRET_KEY_SHA256"), sha256(values["LANGFLOW_SECRET_KEY"].encode()).hexdigest())
        self.assertFalse(any("READY" in l for l in lines))

    def test_real_generators_make_valid_distinct_secrets(self):
        self.init()
        self.init(self.parent / "second")
        first, second = self.env_values(), self.env_values(self.parent / "second")
        for key in install.GENERATED:
            self.assertTrue(install.valid_generated(key, first[key]), key)
            self.assertNotIn("$", first[key])
            self.assertNotEqual(first[key], second[key])
        self.assertEqual(len(__import__("base64").urlsafe_b64decode(first["LANGFLOW_SECRET_KEY"])), 32)
        self.assertEqual(len({first[k] for k in install.GENERATED}), len(install.GENERATED))

    def test_nothing_is_created_when_preflight_refuses(self):
        before = fx.snapshot(self.tmp)
        for sha, target in (("1" * 40, self.target), (self.sha, self.parent / "nope" / "set"), (self.sha, self.repo / "inside")):
            report = install.init_config(sha, str(target), str(self.repo), gen=fx.STUB_GEN)
            self.assertTrue(report.failed)
            self.assertNotIn("CONFIG_PREPARED", report.lines)
        self.assertEqual(fx.snapshot(self.tmp), before)

    def test_the_checkout_is_not_touched(self):
        before = fx.snapshot(self.repo)
        self.init()
        self.assertEqual(fx.snapshot(self.repo), before)
        self.assertFalse((self.repo / ".env").exists())

    def test_the_pair_is_never_visible_half_written(self):
        allowed = {(), tuple(sorted(FILES))}
        for i in range(25):
            target, seen, stop = self.parent / f"w{i}", set(), threading.Event()

            def watch():
                while not stop.is_set():
                    try:
                        seen.add(tuple(sorted(os.listdir(target))))
                    except FileNotFoundError:
                        pass

            watcher = threading.Thread(target=watch)
            watcher.start()
            report = self.init(target, gen=fx.STUB_GEN)
            stop.set()
            watcher.join()
            self.assertFalse(report.failed)
            self.assertLessEqual(seen, allowed)

    def test_unsupported_platform_is_refused_before_anything_is_written(self):
        with mock.patch.object(install, "CAN_PUBLISH", False):
            report = self.init()
        self.assertEqual(report.refused, ["UNSUPPORTED_PLATFORM"])
        self.assertEqual(self.listing(), [])


@posix
class ExistingTargetTests(Base):
    def test_second_run_refuses_and_changes_nothing(self):
        self.init(gen=fx.STUB_GEN)
        before = fx.snapshot(self.parent)
        report = self.init()
        self.assertEqual(self.codes(report), ["TARGET_EXISTS_CONFIG_SET"])
        self.assertEqual(fx.field(report.lines, "EXISTING_KEY_FINGERPRINT"), "MATCH")
        self.assertEqual(fx.snapshot(self.parent), before)
        self.assertEqual(self.env_values()["LANGFLOW_SECRET_KEY"], fx.STUB_VALUES["LANGFLOW_SECRET_KEY"])

    def test_a_changed_key_is_reported_as_mismatch_and_never_regenerated(self):
        self.init(gen=fx.STUB_GEN)
        env = (self.target / ".env").read_text().replace(fx.STUB_VALUES["LANGFLOW_SECRET_KEY"], "A" * 43 + "=")
        (self.target / ".env").write_text(env)
        before = fx.snapshot(self.parent)
        report = self.init()
        self.assertEqual(fx.field(report.lines, "EXISTING_KEY_FINGERPRINT"), "MISMATCH")
        self.assertEqual(report.exit_code(), 2)
        self.assertEqual(fx.snapshot(self.parent), before)

    def test_every_other_existing_target_is_refused_untouched(self):
        def empty(p): p.mkdir()
        def foreign(p): p.mkdir(); (p / "x").write_text("keep")
        def env_only(p): p.mkdir(); (p / ".env").write_text("LANGFLOW_SECRET_KEY=keep-this-key\n")
        def record_only(p): p.mkdir(); (p / "install-record.json").write_text("{}")
        def a_file(p): p.write_text("keep")
        def link_to_dir(p):
            (self.parent / "linked").mkdir(); (self.parent / "linked" / "sentinel").write_text("keep"); os.symlink(self.parent / "linked", p)
        def dangling(p): os.symlink(self.parent / "missing", p)
        for make in (empty, foreign, env_only, record_only, a_file, link_to_dir, dangling):
            target = self.parent / make.__name__
            make(target)
            before = fx.snapshot(self.parent)
            report = self.init(target, gen=fx.STUB_GEN)
            with self.subTest(case=make.__name__):
                self.assertEqual(report.exit_code(), 2, report.lines)
                self.assertTrue(report.refused[0].startswith("TARGET_EXISTS_"))
                self.assertNotIn("CONFIG_PREPARED", report.lines)
                self.assertEqual(fx.snapshot(self.parent), before)
                self.assertNoSecrets(self.text(report))

    def test_partial_config_keeps_its_key_and_is_not_marked_usable(self):
        target = self.parent / "partial"
        target.mkdir()
        (target / ".env").write_text("LANGFLOW_SECRET_KEY=existing-key-1234\n")
        report = self.init(target)
        self.assertEqual(self.codes(report), ["TARGET_EXISTS_PARTIAL_CONFIG"])
        self.assertEqual((target / ".env").read_text(), "LANGFLOW_SECRET_KEY=existing-key-1234\n")
        self.assertEqual(os.listdir(target), [".env"])
        self.assertNotIn("CONFIG_PREPARED", report.lines)


@posix
class FaultTests(Base):
    STEPS = ("begin", "staged_dir", "env_written", "record_written", "before_publish")

    def assertCleanFailure(self, report, before):
        self.assertEqual(report.exit_code(), 3, report.lines)
        self.assertEqual(fx.snapshot(self.parent), before)              # no target, no staging, no temp file
        self.assertNotIn("CONFIG_PREPARED", report.lines)
        self.assertNoSecrets(self.text(report))

    def test_an_io_error_at_any_step_publishes_nothing_and_cleans_up(self):
        before = fx.snapshot(self.parent)

        for step in self.STEPS:
            def fault(s, step=step):
                if s == step:
                    raise OSError(errno.ENOSPC, "disk full with " + fx.STUB_VALUES["POSTGRES_PASSWORD"])

            with self.subTest(step=step):
                report = self.init(gen=fx.STUB_GEN, fault=fault)
                self.assertCleanFailure(report, before)
                self.assertEqual(report.unverified, ["WRITE_FAILED"])
            retry = self.init(gen=fx.STUB_GEN)                          # a clean run afterwards is a normal run
            self.assertFalse(retry.failed, retry.lines)
            for name in os.listdir(self.parent):
                os.chmod(self.parent / name, 0o700)
            import shutil
            shutil.rmtree(self.target)

    def test_an_interrupt_at_any_step_publishes_nothing(self):
        before = fx.snapshot(self.parent)
        for step in self.STEPS:
            def fault(s, step=step):
                if s == step:
                    raise KeyboardInterrupt

            with self.subTest(step=step):
                report = self.init(gen=fx.STUB_GEN, fault=fault)
                self.assertCleanFailure(report, before)
                self.assertEqual(report.unverified, ["INTERRUPTED"])

    def test_failures_after_the_rename_cannot_unpublish(self):
        for exc in (OSError(errno.EIO, "x"), KeyboardInterrupt()):
            target = self.parent / type(exc).__name__

            def fault(s, exc=exc):
                if s == "renamed":
                    raise exc

            report = self.init(target, gen=fx.STUB_GEN, fault=fault)
            with self.subTest(exc=type(exc).__name__):
                self.assertIn("CONFIG_PREPARED", report.lines)
                self.assertEqual(sorted(os.listdir(target)), sorted(FILES))

    def test_fsync_failures(self):
        before = fx.snapshot(self.parent)
        with mock.patch.object(install.os, "fsync", side_effect=OSError(errno.EIO, "x")):
            self.assertCleanFailure(self.init(gen=fx.STUB_GEN), before)
        with mock.patch.object(install, "_fsync_dir", side_effect=OSError(errno.EIO, "x")):
            self.assertCleanFailure(self.init(gen=fx.STUB_GEN), before)
        with mock.patch.object(install, "_fsync_dir", side_effect=[None, OSError(errno.EIO, "x")]):    # only the parent, after the rename
            report = self.init(gen=fx.STUB_GEN)
        self.assertIn("CONFIG_PREPARED", report.lines)
        self.assertTrue(any(l.startswith("DURABILITY_UNCONFIRMED") for l in report.lines))

    def test_rename_failure_removes_the_staging(self):
        before = fx.snapshot(self.parent)
        with mock.patch.object(install, "_rename_noreplace", side_effect=OSError(errno.EXDEV, "x")):
            self.assertCleanFailure(self.init(gen=fx.STUB_GEN), before)

    def test_self_check_failure_publishes_nothing(self):
        before = fx.snapshot(self.parent)
        with mock.patch.object(install, "_verify_staged", return_value=False):
            report = self.init(gen=fx.STUB_GEN)
        self.assertCleanFailure(report, before)
        self.assertEqual(report.unverified, ["STAGED_SET_FAILED_SELF_CHECK"])

    def test_invalid_generated_values_are_refused_before_any_write(self):
        before = fx.snapshot(self.parent)
        report = self.init(gen={**fx.STUB_GEN, "LANGFLOW_SECRET_KEY": lambda: "not-a-fernet-key"})
        self.assertCleanFailure(report, before)
        self.assertEqual(report.unverified, ["GENERATED_VALUE_INVALID"])

    def test_a_target_that_appears_before_the_publication_is_never_replaced(self):
        def fault(s):
            if s == "before_publish":
                self.target.mkdir()
                (self.target / "sentinel").write_text("keep")

        report = self.init(gen=fx.STUB_GEN, fault=fault)
        self.assertEqual(self.codes(report), ["TARGET_EXISTS_FOREIGN_DIRECTORY"])
        self.assertEqual(report.exit_code(), 2)
        self.assertEqual(self.listing(), ["set"])      # our files are emptied (the staging directory stays, announced)
        self.assertEqual(os.listdir(self.target), ["sentinel"])


@posix
class ProcessTests(Base):
    def child(self, target=None, **env):
        full = {**os.environ, **{k: str(v) for k, v in env.items()}}
        return subprocess.Popen([sys.executable, str(CHILD), self.sha, str(target or self.target), str(self.repo)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=full)

    def wait_for(self, path, seconds=30):
        import time
        deadline = time.time() + seconds
        while not Path(path).exists():
            self.assertLess(time.time(), deadline, f"{path} never appeared")
            time.sleep(0.01)

    def stale(self):
        return [n for n in os.listdir(self.parent) if n.startswith(".set.staging-")]

    def test_a_hard_kill_before_the_publication_leaves_only_reported_staging_and_no_target(self):
        for step in ("record_written", "before_publish"):
            for name in os.listdir(self.parent):
                shutil.rmtree(self.parent / name)
            proc = self.child(FAULT_STEP=step, FAULT_MODE="exit")
            proc.communicate(timeout=60)
            with self.subTest(step=step):
                self.assertEqual(proc.returncode, 9)
                self.assertFalse(self.target.exists())
                (stale,) = self.stale()
                stale_before = fx.snapshot(self.parent / stale)
                code, lines = fx.cli("preflight", self.sha, self.target, self.repo)
                self.assertEqual(code, 0)
                self.assertTrue(any(l.startswith("STALE_STAGING") and stale in l for l in lines))
                code, lines = fx.cli("init-config", self.sha, self.target, self.repo)
                self.assertEqual(code, 0, lines)
                self.assertEqual(fx.snapshot(self.parent / stale), stale_before)                  # never deleted, never reused
                self.assertNotEqual(self.env_values()["LANGFLOW_SECRET_KEY"], envfile.read_env(self.parent / stale / ".env")["LANGFLOW_SECRET_KEY"])

    def test_a_hard_kill_right_after_the_rename_leaves_the_complete_set(self):
        proc = self.child(FAULT_STEP="renamed", FAULT_MODE="exit")
        proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 9)
        self.assertEqual(sorted(os.listdir(self.target)), sorted(FILES))
        self.assertEqual(install.key_fingerprint_state(str(self.target)), "MATCH")
        self.assertEqual(self.stale(), [])
        code, lines = fx.cli("init-config", self.sha, self.target, self.repo)
        self.assertEqual((code, fx.field(lines, "EXISTING_KEY_FINGERPRINT")), (2, "MATCH"))

    def test_sigterm_publishes_nothing_and_cleans_up(self):
        before = fx.snapshot(self.parent)
        proc = self.child(FAULT_STEP="record_written", FAULT_MODE="pause", PAUSE_FILE=self.tmp / "paused", GO_FILE=self.tmp / "go")
        self.wait_for(self.tmp / "paused")
        proc.terminate()
        out, _ = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("UNVERIFIED INTERRUPTED", out)
        self.assertEqual(fx.snapshot(self.parent), before)

    def test_a_run_that_loses_the_race_is_refused_and_removes_its_own_staging(self):
        first = self.child(FAULT_STEP="before_publish", FAULT_MODE="pause", PAUSE_FILE=self.tmp / "paused", GO_FILE=self.tmp / "go")
        self.wait_for(self.tmp / "paused")
        code, lines = fx.cli("init-config", self.sha, self.target, self.repo)                     # the second run publishes first
        self.assertEqual(code, 0, lines)
        winner = (self.target / ".env").read_bytes()
        (self.tmp / "go").write_text("go")
        out, _ = first.communicate(timeout=60)
        self.assertEqual(first.returncode, 2, out)
        self.assertIn("REFUSED TARGET_EXISTS_CONFIG_SET", out)
        self.assertEqual((self.target / ".env").read_bytes(), winner)
        self.assertEqual(sorted(os.listdir(self.target)), sorted(FILES))
        self.assertEqual(fx.secret_staging(self.parent), [])                                       # our files are emptied ...
        for name in self.stale():                                                                   # ... the emptied directory stays and is announced by its full path
            self.assertIn("STAGING_LEFT_EMPTIED: " + str(self.parent / name), out)                  # the full path, not the bare name
        for key in install.GENERATED:
            self.assertNotIn(self.env_values()[key], out)

    def test_many_simultaneous_runs_produce_exactly_one_set(self):
        procs = [self.child(START_FILE=self.tmp / "start") for _ in range(6)]
        (self.tmp / "start").write_text("go")
        results = [(p.communicate(timeout=120)[0], p.returncode) for p in procs]
        codes = sorted(rc for _, rc in results)
        self.assertEqual(codes, [0, 2, 2, 2, 2, 2], results)
        self.assertEqual(fx.secret_staging(self.parent), [])
        self.assertEqual([n for n in os.listdir(self.parent) if n != "set"], fx.emptied_staging(self.parent))      # losers emptied their files; only emptied staging dirs may stay
        self.assertEqual(self.listing(), ["set"])
        self.assertEqual(sorted(os.listdir(self.target)), sorted(FILES))
        self.assertEqual(install.key_fingerprint_state(str(self.target)), "MATCH")
        winner = next(out for out, rc in results if rc == 0)
        losers = "\n".join(out for out, rc in results if rc != 0)
        for key in install.GENERATED:
            self.assertNotIn(self.env_values()[key], winner + losers)


class ParityTests(Base):
    def test_the_four_secrets_and_their_formats_are_the_ones_bootstrap_generates(self):
        import bootstrap
        self.assertEqual(set(install.GENERATED), set(bootstrap.GENERATED_SECRETS))
        for key in install.GENERATED:
            for make in (install.GENERATORS[key], bootstrap.GENERATED_SECRETS[key]):
                self.assertTrue(install.valid_generated(key, make()), key)

    def test_placeholder_rule_is_the_one_of_envfile(self):
        self.assertIs(install.is_placeholder, envfile.is_placeholder)
        self.assertEqual(envfile.PLACEHOLDER_PREFIX, "CHANGE_ME")

    def test_the_fingerprint_check_reads_only_the_recorded_key(self):
        fx.write_config_set(self.parent / "a", "k" * 44)
        fx.write_config_set(self.parent / "b", "k" * 44, record_key_sha=re.sub(".", "0", "x" * 64))
        self.assertEqual(install.key_fingerprint_state(str(self.parent / "a")), "MATCH")
        self.assertEqual(install.key_fingerprint_state(str(self.parent / "b")), "MISMATCH")
        self.assertEqual(install.key_fingerprint_state(str(self.parent / "missing")), "UNVERIFIABLE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
