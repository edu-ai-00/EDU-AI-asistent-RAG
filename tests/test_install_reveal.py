"""B06-1 install.py: the one-time overview of the secrets a successful init-config generated.

The protected .env stays the permanent source. The four values THIS run generated are shown once, in the final overview, and only
in an interactive terminal (stdout and stdin are ttys, no CI environment); anywhere else the output carries the path alone.
Existing values, supplied tokens and provider keys are never shown, and no error or log line may hold a value.

Run: python tests/test_install_reveal.py   (portable part: any OS with git; init-config and pty parts need Linux)
Assertions name the secret that leaked, never its value. Synthetic data and temporary directories only.
"""
import contextlib
import io
import os
import select
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

import envfile  # noqa: E402

SCRIPT = Path(install.__file__)
MARKERS = list(install.MARKERS)
posix = unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux (modes, renameat2, pty)")

SUPPLIED = {"OPENAI_API_KEY": "sk-synthetic-supplied-0123456789", "CLOUDFLARE_TUNNEL_TOKEN": "cf-synthetic-token-0123456789",
            "JINA_API_KEY": "jina-synthetic-key-0123456789"}


class FakeStream(io.StringIO):
    def __init__(self, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@contextlib.contextmanager
def terminal(stdout_tty=True, stdin_tty=True, **env):
    """Run the body as if stdout/stdin were (not) ttys and the given environment variables were set; yields the stdout stream."""
    out = FakeStream(stdout_tty)
    with mock.patch.dict(os.environ), contextlib.redirect_stdout(out), mock.patch.object(sys, "stdin", FakeStream(stdin_tty)):
        for name in install._CI_FLAGS:
            os.environ.pop(name, None)
        os.environ.update(env)
        yield out


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

    def init(self, **kw):
        return install.init_config(self.sha, str(self.target), str(self.repo), **kw)

    def argv(self, cmd):
        return [cmd, "--source-sha", self.sha, "--target-dir", str(self.target), "--repo", str(self.repo)]

    def env_values(self):
        return envfile.read_env(self.target / ".env")

    def assertHidden(self, text, values, what):
        leaked = sorted(name for name, value in values.items() if value in text)
        self.assertEqual(leaked, [], f"{what}: the value of these secrets reached the output (names only are shown here)")

    def assertShownOnce(self, lines, values):
        """The block is the last thing before the markers: header, then the four NAME=value lines in the fixed order."""
        self.assertEqual(lines[-2:], MARKERS)
        block = lines[-2 - len(install.GENERATED) - 1:-2]
        self.assertTrue(block[0].startswith("GENERATED_SECRETS: "), block[0])
        self.assertEqual([l.split("=", 1)[0] for l in block[1:]], list(install.GENERATED))
        for line in block[1:]:
            name, value = line.split("=", 1)
            self.assertTrue(value == values[name], f"{name}: shown value differs from the generated one")
        self.assertHidden("\n".join(lines[:-2 - len(install.GENERATED) - 1]), values, "before the final block")
        self.assertEqual("\n".join(lines).count("GENERATED_SECRETS: "), 1, "the header must appear exactly once")


class DecisionTests(unittest.TestCase):
    """interactive_terminal: only a person at a terminal."""

    def test_both_ttys_and_no_ci_is_interactive(self):
        self.assertTrue(install.interactive_terminal(FakeStream(True), FakeStream(True), {}))

    def test_a_redirected_stdout_or_stdin_is_not_interactive(self):
        for out, inp in ((False, True), (True, False), (False, False)):
            with self.subTest(stdout_tty=out, stdin_tty=inp):
                self.assertFalse(install.interactive_terminal(FakeStream(out), FakeStream(inp), {}))

    def test_a_stream_that_cannot_say_is_not_interactive(self):
        class Closed:
            def isatty(self):
                raise ValueError("I/O operation on closed file")
        for stream in (object(), Closed()):
            with self.subTest(stream=type(stream).__name__):
                self.assertFalse(install.interactive_terminal(stream, FakeStream(True), {}))
                self.assertFalse(install.interactive_terminal(FakeStream(True), stream, {}))

    def test_any_ci_environment_hides_it_even_on_a_tty(self):
        for name in install._CI_FLAGS:
            for value in ("true", "1", "yes", "anything"):
                with self.subTest(name=name, value=value):
                    self.assertFalse(install.interactive_terminal(FakeStream(True), FakeStream(True), {name: value}))

    def test_a_ci_variable_that_says_no_is_not_ci(self):
        for value in ("", "0", "false", "FALSE", "no", "  "):
            with self.subTest(value=value):
                self.assertTrue(install.interactive_terminal(FakeStream(True), FakeStream(True), {"CI": value}))

    def test_the_defaults_read_the_live_streams_and_environment(self):
        with terminal(True, True):
            self.assertTrue(install.interactive_terminal())
        with terminal(True, True, CI="true"):
            self.assertFalse(install.interactive_terminal())
        with terminal(False, True):
            self.assertFalse(install.interactive_terminal())
        with terminal(True, False):
            self.assertFalse(install.interactive_terminal())


class EmitTests(unittest.TestCase):
    """Report.emit: where the block goes, once, and never on a failure."""

    def report(self, reveal=None, *, fail=None):
        r = install.Report()
        r.say("CONFIG_PREPARED")
        r.say("CONFIG_FILE: /srv/cfg/.env")
        if fail == "refused":
            r.refuse("TARGET_EXISTS_CONFIG_SET")
        elif fail == "unverified":
            r.unverifiable("PUBLICATION_STATE_UNKNOWN")
        r.reveal = dict(reveal) if reveal is not None else None
        return r

    def lines(self, r, **kw):
        out = io.StringIO()
        r.emit(out, **kw)
        return out.getvalue().splitlines()

    def assertShown(self, lines):
        block = lines[-2 - len(install.GENERATED) - 1:-2]
        self.assertEqual(lines[-2:], MARKERS)
        self.assertTrue(block[0].startswith("GENERATED_SECRETS: "))
        self.assertEqual(block[1:], [f"{k}={fx.STUB_VALUES[k]}" for k in install.GENERATED])

    def test_interactive_shows_the_four_values_just_before_the_markers(self):
        lines = self.lines(self.report(fx.STUB_VALUES), interactive=True)
        self.assertShown(lines)
        self.assertEqual(lines[:2], ["CONFIG_PREPARED", "CONFIG_FILE: /srv/cfg/.env"])
        before_block = "\n".join(lines[:-2 - len(install.GENERATED) - 1])
        for name, value in fx.STUB_VALUES.items():
            self.assertTrue(value not in before_block, f"{name} appears before the final block")
        self.assertNotIn("NOT_SHOWN", "\n".join(lines))
        self.assertIn("not shown again", lines[-2 - len(install.GENERATED) - 1])

    def test_it_is_one_time(self):
        r = self.report(fx.STUB_VALUES)
        self.assertEqual(len(self.lines(r, interactive=True)), 2 + 1 + len(install.GENERATED) + 2)
        self.assertIsNone(r.reveal)
        again = "\n".join(self.lines(r, interactive=True))
        for name, value in fx.STUB_VALUES.items():
            self.assertTrue(value not in again, f"{name} printed a second time")
        self.assertNotIn("GENERATED_SECRETS", again)

    def test_not_interactive_shows_only_the_path_and_says_why(self):
        lines = self.lines(self.report(fx.STUB_VALUES), interactive=False)
        text = "\n".join(lines)
        for name, value in fx.STUB_VALUES.items():
            self.assertTrue(value not in text, f"{name} leaked into a non-interactive output")
        self.assertEqual(lines[-2:], MARKERS)
        self.assertTrue(lines[-3].startswith("GENERATED_SECRETS_NOT_SHOWN: "))
        self.assertIn("CONFIG_FILE: /srv/cfg/.env", lines)
        self.assertNotIn("GENERATED_SECRETS: ", text)

    def test_nothing_is_shown_for_a_report_that_failed(self):
        for fail in ("refused", "unverified"):
            with self.subTest(fail=fail):
                for interactive in (True, False):
                    text = "\n".join(self.lines(self.report(fx.STUB_VALUES, fail=fail), interactive=interactive))
                    for name, value in fx.STUB_VALUES.items():
                        self.assertTrue(value not in text, f"{name} leaked from a failed report")
                    self.assertNotIn("GENERATED_SECRETS", text)

    def test_a_report_without_values_has_no_overview_at_all(self):
        for interactive in (True, False):
            self.assertNotIn("GENERATED_SECRETS", "\n".join(self.lines(self.report(None), interactive=interactive)))

    def test_only_the_four_generated_names_can_ever_be_printed(self):
        extra = {**fx.STUB_VALUES, **SUPPLIED, "LANGFLOW_API_KEY": "lf-issued-later-0123456789"}
        text = "\n".join(self.lines(self.report(extra), interactive=True))
        for name, value in {**SUPPLIED, "LANGFLOW_API_KEY": "lf-issued-later-0123456789"}.items():
            self.assertTrue(value not in text, f"{name} is not a generated secret and must never be shown")

    def test_the_default_wiring_follows_the_live_terminal_and_environment(self):
        for tty_out, tty_in, env, shown in ((True, True, {}, True), (True, True, {"CI": "true"}, False),
                                            (True, False, {}, False), (False, True, {}, False)):
            with self.subTest(stdout_tty=tty_out, stdin_tty=tty_in, env=env):
                with terminal(tty_out, tty_in, **env) as out:
                    self.report(fx.STUB_VALUES).emit()
                text = out.getvalue()
                self.assertEqual(all(v in text for v in fx.STUB_VALUES.values()), shown)
                self.assertEqual(text.splitlines()[-2:], MARKERS)


@posix
class InitConfigTests(Base):
    """In-process init-config and main under a faked terminal."""

    def test_a_clean_success_holds_exactly_the_four_generated_values_apart_from_the_lines(self):
        report = self.init(gen=fx.STUB_GEN)
        self.assertFalse(report.failed, report.lines)
        self.assertEqual(report.reveal, fx.STUB_VALUES)
        self.assertHidden("\n".join(report.lines), fx.STUB_VALUES, "report lines")
        self.assertEqual({k: self.env_values()[k] for k in install.GENERATED}, fx.STUB_VALUES)

    def test_interactive_main_shows_what_the_protected_env_holds_and_leaves_it_alone(self):
        with terminal() as out:
            code = install.main(self.argv("init-config"))
        lines = out.getvalue().splitlines()
        self.assertEqual(code, 0, lines)
        values = {k: self.env_values()[k] for k in install.GENERATED}
        self.assertShownOnce(lines, values)
        for name, value in values.items():
            self.assertTrue(install.valid_generated(name, value), name)
        self.assertEqual(oct(os.stat(self.target / ".env").st_mode & 0o7777), "0o600")
        self.assertEqual(oct(os.stat(self.target).st_mode & 0o7777), "0o700")
        self.assertEqual(sorted(os.listdir(self.target)), [".env", "install-record.json"])      # the display writes nothing
        self.assertHidden((self.target / "install-record.json").read_text(), values, "install-record.json")

    def test_non_interactive_main_prints_the_path_and_no_value(self):
        for label, kwargs in (("redirected stdout", {"stdout_tty": False}), ("redirected stdin", {"stdin_tty": False}),
                              ("CI", {"CI": "true"}), ("GitHub Actions", {"GITHUB_ACTIONS": "true"})):
            with self.subTest(label):
                self.target = self.parent / label.replace(" ", "_")
                with terminal(**kwargs) as out:
                    code = install.main(self.argv("init-config"))
                lines = out.getvalue().splitlines()
                self.assertEqual(code, 0, lines)
                values = {k: self.env_values()[k] for k in install.GENERATED}
                self.assertHidden("\n".join(lines), values, label)
                self.assertEqual(fx.field(lines, "CONFIG_FILE"), str(self.target / ".env"))
                self.assertTrue(any(l.startswith("GENERATED_SECRETS_NOT_SHOWN: ") for l in lines))
                self.assertEqual(lines[-2:], MARKERS)

    def test_supplied_tokens_and_provider_keys_are_never_shown(self):
        template = fx.SYN_TEMPLATE.replace("OPENAI_API_KEY=CHANGE_ME_openai", f"OPENAI_API_KEY={SUPPLIED['OPENAI_API_KEY']}")
        template += "".join(f"{k}={v}\n" for k, v in SUPPLIED.items() if k != "OPENAI_API_KEY")
        self.repo, self.sha = fx.make_repo(self.tmp / "repo2", files={"docker-compose.yml": fx.SYN_COMPOSE, ".env.example": template})
        with terminal() as out:
            code = install.main(self.argv("init-config"))
        lines = out.getvalue().splitlines()
        self.assertEqual(code, 0, lines)
        env = self.env_values()
        self.assertEqual({k: env[k] for k in SUPPLIED}, SUPPLIED)                    # kept in the file, as supplied
        self.assertHidden("\n".join(lines), SUPPLIED, "supplied tokens and provider keys")
        self.assertShownOnce(lines, {k: env[k] for k in install.GENERATED})

    def test_a_repeated_run_shows_nothing_and_leaves_the_set_alone(self):
        with terminal() as out:
            self.assertEqual(install.main(self.argv("init-config")), 0)
        values = {k: self.env_values()[k] for k in install.GENERATED}
        before = fx.snapshot(self.target)
        with terminal() as out:
            code = install.main(self.argv("init-config"))
        text = out.getvalue()
        self.assertEqual(code, 2)
        self.assertHidden(text, values, "a repeated run")
        self.assertNotIn("GENERATED_SECRETS", text)
        self.assertEqual(fx.snapshot(self.target), before)

    def test_preflight_and_plan_never_show_anything(self):
        for cmd in ("preflight", "plan"):
            with self.subTest(cmd), terminal() as out:
                self.assertEqual(install.main(self.argv(cmd)), 0)
                self.assertNotIn("GENERATED_SECRETS", out.getvalue())
        self.assertFalse(self.target.exists())

    def test_a_refused_run_shows_nothing(self):
        (self.target).write_text("someone else's file")
        values = dict(fx.STUB_VALUES)
        report = self.init(gen=fx.STUB_GEN)
        self.assertTrue(report.failed)
        self.assertIsNone(report.reveal)
        out = io.StringIO()
        report.emit(out, interactive=True)
        self.assertHidden(out.getvalue(), values, "a refused run")
        self.assertNotIn("GENERATED_SECRETS", out.getvalue())

    def test_a_failure_at_any_step_shows_nothing_even_when_the_error_text_holds_a_value(self):
        for step in ("begin", "staged_dir", "env_written", "record_written", "before_publish"):
            with self.subTest(step=step):
                def fault(at, step=step):
                    if at == step:
                        raise RuntimeError("a bug that quotes " + fx.STUB_VALUES["POSTGRES_PASSWORD"])
                report = self.init(gen=fx.STUB_GEN, fault=fault)
                self.assertTrue(report.failed)
                self.assertIsNone(report.reveal)
                out = io.StringIO()
                report.emit(out, interactive=True)
                self.assertHidden(out.getvalue(), fx.STUB_VALUES, f"a failure at {step}")
                self.assertNotIn("GENERATED_SECRETS", out.getvalue())
                self.assertFalse(self.target.exists())

    def test_an_interrupted_or_failed_run_after_the_publication_shows_nothing(self):
        # The set is complete on disk (CONFIG_PREPARED), but the run was not a clean success: the values are read from the file.
        for label, exc in (("signal", KeyboardInterrupt()), ("bug", RuntimeError("after the rename"))):
            with self.subTest(label):
                self.target = self.parent / label
                def fault(at, exc=exc):
                    if at == "renamed":
                        raise exc
                report = self.init(gen=fx.STUB_GEN, fault=fault)
                self.assertIn("CONFIG_PREPARED", report.lines)
                self.assertIsNone(report.reveal)
                out = io.StringIO()
                report.emit(out, interactive=True)
                self.assertHidden(out.getvalue(), fx.STUB_VALUES, label)
                self.assertNotIn("GENERATED_SECRETS", out.getvalue())
                self.assertEqual({k: self.env_values()[k] for k in install.GENERATED}, fx.STUB_VALUES)


def run_in_pty(args, env, *, stdin_tty=True, stdout_tty=True):
    """Run install.py with a real pseudo-terminal on stdin/stdout/stderr as asked; returns (exit code, text)."""
    import pty
    master, slave = pty.openpty()
    stdin = slave if stdin_tty else subprocess.DEVNULL
    stdout = slave if stdout_tty else subprocess.PIPE
    proc = subprocess.Popen([sys.executable, str(SCRIPT), *args], stdin=stdin, stdout=stdout, stderr=slave, env=env, close_fds=True)
    os.close(slave)
    chunks, deadline = [], time.time() + 90
    try:
        if stdout_tty:
            while True:
                ready, _, _ = select.select([master], [], [], 0.2)
                if ready:
                    try:
                        data = os.read(master, 65536)
                    except OSError:                      # EIO: the child closed its side
                        break
                    if not data:
                        break
                    chunks.append(data)
                elif proc.poll() is not None:
                    break
                if time.time() > deadline:
                    proc.kill()
                    break
            proc.wait(timeout=30)
            text = b"".join(chunks).decode("utf-8", "replace").replace("\r\n", "\n")
        else:
            piped, _ = proc.communicate(timeout=90)
            text = piped.decode("utf-8", "replace")
    finally:
        os.close(master)
    return proc.returncode, text


@posix
class RealTerminalTests(Base):
    """The whole command line, through a real pseudo-terminal."""

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if k not in install._CI_FLAGS}
        env.update(PYTHONDONTWRITEBYTECODE="1", **extra)
        return env

    def go(self, cmd="init-config", **kw):
        env = kw.pop("env", None) or self.env()
        return run_in_pty(self.argv(cmd), env, **kw)

    def generated(self):
        return {k: self.env_values()[k] for k in install.GENERATED}

    def test_in_a_terminal_the_four_generated_values_are_shown_once_and_match_the_env(self):
        code, text = self.go()
        lines = text.splitlines()
        self.assertEqual(code, 0, [l for l in lines if "=" not in l])
        values = self.generated()
        self.assertShownOnce(lines, values)
        self.assertEqual(oct(os.stat(self.target / ".env").st_mode & 0o7777), "0o600")
        self.assertHidden((self.target / "install-record.json").read_text(), values, "install-record.json")

    def test_a_pipe_gets_the_path_and_no_value(self):
        code, text = self.go(stdout_tty=False)
        self.assertEqual(code, 0)
        values = self.generated()
        self.assertHidden(text, values, "piped output")
        self.assertIn(f"CONFIG_FILE: {self.target / '.env'}", text.splitlines())
        self.assertTrue(any(l.startswith("GENERATED_SECRETS_NOT_SHOWN: ") for l in text.splitlines()))

    def test_a_terminal_without_a_tty_on_stdin_gets_no_value(self):
        code, text = self.go(stdin_tty=False)
        self.assertEqual(code, 0)
        self.assertHidden(text, self.generated(), "no tty on stdin")
        self.assertNotIn("GENERATED_SECRETS: ", text)

    def test_ci_gets_no_value_even_with_a_terminal(self):
        for name in ("CI", "GITHUB_ACTIONS", "JENKINS_URL"):
            with self.subTest(name):
                self.target = self.parent / name
                code, text = self.go(env=self.env(**{name: "true"}))
                self.assertEqual(code, 0)
                self.assertHidden(text, self.generated(), name)
                self.assertNotIn("GENERATED_SECRETS: ", text)
                self.assertEqual(text.splitlines()[-2:], MARKERS)

    def test_a_repeated_run_in_a_terminal_shows_no_existing_value(self):
        self.assertEqual(self.go()[0], 0)
        values = self.generated()
        before = fx.snapshot(self.target)
        code, text = self.go()
        self.assertEqual(code, 2)
        self.assertHidden(text, values, "a repeated run in a terminal")
        self.assertNotIn("GENERATED_SECRETS", text)
        self.assertEqual(fx.snapshot(self.target), before)

    def test_preflight_and_plan_in_a_terminal_show_nothing(self):
        for cmd in ("preflight", "plan"):
            with self.subTest(cmd):
                code, text = self.go(cmd)
                self.assertEqual(code, 0)
                self.assertNotIn("GENERATED_SECRETS", text)
        self.assertFalse(self.target.exists())

    def test_a_refused_start_in_a_terminal_shows_nothing(self):
        self.target.write_text("someone else's file")
        code, text = self.go()
        self.assertEqual(code, 2)
        self.assertNotIn("GENERATED_SECRETS", text)
        self.assertEqual(self.target.read_text(), "someone else's file")


if __name__ == "__main__":
    unittest.main()
