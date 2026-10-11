"""B06 bootstrap: the Langflow API key lifecycle after the final review (findings 3, 4 and 5).

Run: python tests/test_bootstrap_key_lifecycle.py   (add -v for names)

Offline, like test_bootstrap_key_rotation.py: HTTP is the fake of test_bootstrap_key_safety.py (a mock of urllib,
NOT Langflow 1.10.1), the .env is synthetic and temporary, no socket, subprocess or Docker. The SIGINT cases raise
a REAL signal with the default handler installed. Synthetic values only; no key, id or masked value may appear
in any message.

What it pins down
  * finding 3: before .env is written a failure deletes the new key; once the write began the new key is deleted
    only when a fresh read of .env proves it was not written (equal bytes); a readback failure, a SIGINT after
    the rename, an unreadable or a foreign .env keep the key and say what is unknown (KeyStateUncertain, exit 1,
    never 3); ".env is unchanged" is said only after the bytes were compared;
  * finding 4: an HTTP 403 on the check of the key in .env is "unverifiable" (one request, exit 3, .env untouched),
    a 401 still replaces, --force still skips the check, a 403 on the NEW key deletes it again;
  * finding 5: no old key is ever deleted (a masked prefix identifies nobody); a failed listing is "unknown", not
    "empty"; the new key is deleted only by the reply's id or as the one fresh entry between two good listings.
"""
import contextlib
import errno
import os
import pathlib
import signal
import sys
import unittest
import urllib.error
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import test_bootstrap_key_safety as ks  # noqa: E402  (modules, not their classes: nothing is collected twice)
import test_bootstrap_key_rotation as kr  # noqa: E402
import bootstrap  # noqa: E402
import create_api_key as cak  # noqa: E402

SCRIPTS = HERE.parent / "scripts"
BASE, OLD_KEY, NEW_KEY, PASTED_KEY = ks.BASE, ks.OLD_KEY, ks.NEW_KEY, ks.PASTED_KEY
THIRD_KEY = "sk-syn-third-key-hhhhhhhhhhhhhhhh"
OLD_MASK, NEW_MASK, OTHER_MASK = kr.OLD_MASK, kr.NEW_MASK, kr.OTHER_MASK
KEYS_PATH, FLOWS_CHECK = kr.KEYS_PATH, kr.FLOWS_CHECK
OLD_ID, NEW_ID, OTHER_ID, EXTRA_ID = ("synthetic-id-old-0001", "synthetic-id-new-0002",
                                      "synthetic-id-other-0003", "synthetic-id-extra-0004")
IDS_AND_MASKS = (OLD_ID, NEW_ID, OTHER_ID, EXTRA_ID, OLD_MASK, NEW_MASK, OTHER_MASK, THIRD_KEY)
DENIED = "HTTP 403, access denied: not proof that the key is invalid"
NOTICE_PREVIOUS = "previous key was left in Langflow"
NOTICE_NEW_LEFT = f"A key named {cak.KEY_NAME} was created in this run and left in Langflow"

BEFORE = [(OLD_ID, cak.KEY_NAME, OLD_MASK), (OTHER_ID, "someone-elses", OTHER_MASK)]
AFTER = BEFORE + [(NEW_ID, cak.KEY_NAME, NEW_MASK)]


def status_of(code):
    """The process exit status behind SystemExit.code: None is 0, a message is 1."""
    return 0 if code is None else code if isinstance(code, int) else 1


@contextlib.contextmanager
def default_sigint():
    """A real SIGINT must raise KeyboardInterrupt, whatever runner is in charge."""
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def replace_then_sigint():
    """os.replace runs for real, then a real SIGINT arrives: the write is done, the caller does not know."""
    real = os.replace

    def replace(source, target):
        real(source, target)
        signal.raise_signal(signal.SIGINT)
    return mock.patch("os.replace", side_effect=replace)


def sigint_instead_of_replace():
    """A real SIGINT arrives just before the rename, which then never runs."""
    return mock.patch("os.replace", side_effect=lambda source, target: signal.raise_signal(signal.SIGINT))


class FullDisk:
    """A temp file whose write fails the way an exceeded file-size limit does."""

    def __init__(self, handle):
        self._handle = handle

    def __enter__(self):
        self._handle.__enter__()
        return self

    def __exit__(self, *exc):
        return self._handle.__exit__(*exc)

    def fileno(self):
        return self._handle.fileno()

    def write(self, data):
        raise OSError(errno.EFBIG, f"File too large {NEW_KEY}")


def failing_temp_write():
    real = os.fdopen
    return mock.patch("os.fdopen", side_effect=lambda *a, **kw: FullDisk(real(*a, **kw)))


class Lifecycle(kr.Rotation):
    def modes(self):
        """(label, ensure_api_key kwargs, command-line arguments, prepare): the paths that reach a new key."""
        yield "replaced (401)", {}, [], self.reject_old_key
        yield "--force", {"force": True}, ["--force"], lambda: None
        yield "first creation", {}, [], lambda: self.write_env(LANGFLOW_API_KEY=None)

    def script_listing(self, before=BEFORE, after=AFTER):
        """The key listing: `before` until a key was created, `after` once one was. Each is a list of
        (id, name, masked value), a FakeResponse, or an exception."""
        def entries(items):
            return ks.json_response({"api_keys": [{"id": i, "name": n, "api_key": m} for i, n, m in items]})

        def answer():
            created = any(c.method == "POST" and c.path == KEYS_PATH for c in self.lf.calls)
            what = after if created else before
            if isinstance(what, BaseException):
                raise what
            return what if isinstance(what, ks.FakeResponse) else entries(what)
        self.lf.routes[("GET", KEYS_PATH)] = answer

    def scene(self, prepare=lambda: None, *, reply_id=NEW_ID, before=BEFORE, after=AFTER):
        """A fresh .env, an empty call record, the listing and the creation reply of one scenario."""
        self.lf.calls.clear()
        self.lf.flows_by_key.clear()
        self.write_env()
        prepare()
        self.script_listing(before, after)
        reply = {"api_key": NEW_KEY, **({"id": reply_id} if reply_id else {})}
        self.lf.routes[("POST", KEYS_PATH)] = ks.json_response(reply)

    def reply_without_id(self):
        self.lf.routes[("POST", KEYS_PATH)] = ks.json_response({"api_key": NEW_KEY})

    def refuse_readback(self, key=NEW_KEY):
        """read_env as create_api_key sees it: it refuses once .env holds `key` (the readback after the write)."""
        real = cak.read_env

        def read_env(path):
            values = real(path)
            if values.get("LANGFLOW_API_KEY") == key:
                raise PermissionError(errno.EACCES, f"denied {key}")
            return values
        return mock.patch.object(cak, "read_env", read_env)

    def flaky_fresh_read(self):
        """Path.read_bytes of .env: the first read (before the write) works, every later one is refused."""
        original, reads = pathlib.Path.read_bytes, []

        def read_bytes(path):
            if path == self.env_path:
                reads.append(path)
                if len(reads) > 1:
                    raise PermissionError(errno.EACCES, "denied")
            return original(path)
        return mock.patch.object(pathlib.Path, "read_bytes", read_bytes)

    def attempt(self, error, kwargs, *patchers):
        """ensure_api_key under `patchers`, expecting `error`; nobody may be prompted."""
        with contextlib.ExitStack() as stack:
            for patcher in patchers:
                stack.enter_context(patcher)
            prompt = stack.enter_context(self.no_prompt(tty=True))
            out = stack.enter_context(self.capture())
            with self.assertRaises(error) as caught:
                self.run_ensure(allow_prompt=True, **kwargs)
        prompt.assert_not_called()
        return caught.exception, out

    def run_cli(self, argv, *patchers):
        code = None
        with contextlib.ExitStack() as stack:
            for patcher in patchers:
                stack.enter_context(patcher)
            stack.enter_context(mock.patch.object(sys, "argv", ["create_api_key.py", "--url", BASE, *argv]))
            prompt = stack.enter_context(self.no_prompt(tty=True))
            out = stack.enter_context(self.capture())
            try:
                cak.main()
            except SystemExit as exc:
                code = exc.code
        prompt.assert_not_called()
        return code, out

    def run_bootstrap(self, *patchers):
        with contextlib.ExitStack() as stack:
            for patcher in patchers:
                stack.enter_context(patcher)
            argv = ["bootstrap.py", "--no-input", "--langflow-url", BASE]
            stack.enter_context(mock.patch.object(sys, "argv", argv))
            for name in ("step_preflight", "step_env", "step_dirs"):
                stack.enter_context(mock.patch.object(bootstrap, name))
            up = stack.enter_context(mock.patch.object(bootstrap, "step_up", return_value=True))
            later = [stack.enter_context(mock.patch.object(bootstrap, name))
                     for name in ("step_langflow_vars", "step_flows", "step_smoke")]
            out = stack.enter_context(self.capture())
            code = bootstrap.main()
        return code, up, later, out

    # assertions ------------------------------------------------------------------------------------------
    def assert_no_state_leak(self, text):
        self.assert_clean(text)
        for token in IDS_AND_MASKS:
            self.assertNotIn(token, text)

    def assert_nothing_deleted(self):
        self.assertEqual([c for c in self.lf.calls if c.method == "DELETE"], [])

    def assert_untouched(self, before, listing):
        self.assertEqual(self.env_state(), before)                                # bytes and mtime
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), listing)     # no temporary file

    def snapshot(self):
        return self.env_state(), sorted(p.name for p in self.tmp.iterdir())


# ───────────────────────────────────────────────────────────────
#  finding 3: a failure before the publication vs after it
# ───────────────────────────────────────────────────────────────

class PublishedStateTests(Lifecycle):
    """The write reached .env (or may have): the new key is never deleted."""

    def test_a_readback_failure_after_the_write_keeps_the_new_key_and_says_so(self):
        for label, kwargs, argv, prepare in self.modes():
            with self.subTest(label):
                self.scene(prepare)
                exc, out = self.attempt(cak.KeyStateUncertain, kwargs, self.refuse_readback())
                self.assert_nothing_deleted()                                     # neither the new key nor an old one
                self.assertEqual(self.key_in_env(), NEW_KEY)
                self.assertIsInstance(exc, cak.KeyStoreError)
                self.assertNotIsInstance(exc, cak.KeyUnverifiable)
                text = str(exc).lower()
                for word in ("unchanged", "not saved"):
                    self.assertNotIn(word, text)
                self.assertIn("stored in .env", text)
                self.assertIn("left there", text)
                self.assert_no_state_leak(str(exc) + out.text)

    def test_the_command_exits_1_not_3_and_prints_the_state(self):
        for label, kwargs, argv, prepare in self.modes():
            with self.subTest(label):
                self.scene(prepare)
                code, out = self.run_cli(argv, self.refuse_readback())
                self.assertEqual(status_of(code), 1)
                self.assertNotEqual(status_of(code), cak.EXIT_RETRY_LATER)
                self.assertIsInstance(code, str)
                self.assertTrue(code.startswith("ERROR:"))
                self.assertIn("docker compose up -d", code)
                self.assertIn("LANGFLOW_API_KEY", code)
                self.assertNotIn("unchanged", code)
                self.assert_nothing_deleted()
                self.assertEqual(self.key_in_env(), NEW_KEY)
                self.assert_no_state_leak(str(code) + out.text)

    def test_a_real_sigint_right_after_the_rename_keeps_the_new_key_and_propagates(self):
        for label, kwargs, argv, prepare in self.modes():
            with self.subTest(label):
                self.scene(prepare)
                with default_sigint():
                    exc, out = self.attempt(KeyboardInterrupt, kwargs, replace_then_sigint())
                self.assertIs(type(exc), KeyboardInterrupt)                       # re-raised as it was
                self.assert_nothing_deleted()
                self.assertEqual(self.key_in_env(), NEW_KEY)
                self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), [".env"])
                self.assertIn("stored in .env", out.stderr)
                self.assertNotIn("unchanged", out.stderr)
                self.assertIn("docker compose up -d", out.stderr)
                self.assert_no_state_leak(out.text)

    def test_a_real_sigint_before_the_rename_proves_nothing_was_written_and_deletes_the_new_key(self):
        for label, kwargs, argv, prepare in self.modes():
            with self.subTest(label):
                self.scene(prepare)
                before = self.snapshot()
                with default_sigint():
                    exc, out = self.attempt(KeyboardInterrupt, kwargs, sigint_instead_of_replace())
                self.assertIs(type(exc), KeyboardInterrupt)
                self.assert_untouched(*before)
                self.assertEqual([c.path for c in self.lf.calls if c.method == "DELETE"], [KEYS_PATH + NEW_ID])
                self.assertIn(".env is unchanged (compared byte for byte)", out.stderr)    # the bytes were compared
                self.assertIn("removed from Langflow again", out.stderr)
                self.assert_no_state_leak(out.text)

    def test_a_fresh_read_that_fails_leaves_the_state_unknown_and_deletes_nothing(self):
        for label, kwargs, argv, prepare in self.modes():
            with self.subTest(label):
                self.scene(prepare)
                before = self.snapshot()
                # The rename really failed, but nobody could look afterwards: only the disk may decide.
                with mock.patch("os.replace", side_effect=OSError(errno.EACCES, f"denied {NEW_KEY}")):
                    exc, out = self.attempt(cak.KeyStateUncertain, kwargs, self.flaky_fresh_read())
                self.assert_untouched(*before)
                self.assert_nothing_deleted()
                self.assertIn("unknown", str(exc))
                self.assertNotIn("unchanged", str(exc))
                self.assertIn("left there", str(exc))
                self.assert_no_state_leak(str(exc) + out.text)

    def test_a_fresh_read_that_shows_a_third_value_is_unknown_and_deletes_nothing(self):
        def foreign_write_then_failure(source, target):
            pathlib.Path(target).write_bytes(ks.synthetic_env(LANGFLOW_API_KEY=THIRD_KEY))
            raise OSError(errno.EIO, "I/O error")

        for label, kwargs, argv, prepare in self.modes():
            with self.subTest(label):
                self.scene(prepare)
                with mock.patch("os.replace", side_effect=foreign_write_then_failure):
                    exc, out = self.attempt(cak.KeyStateUncertain, kwargs)
                self.assertEqual(self.key_in_env(), THIRD_KEY)
                self.assert_nothing_deleted()
                self.assertIn("does not hold the key", str(exc))
                self.assertNotIn("unchanged", str(exc))
                self.assertNotIn(THIRD_KEY, str(exc) + out.text)
                self.assert_no_state_leak(str(exc) + out.text)

    def test_an_interrupt_with_an_unreadable_file_afterwards_is_unknown(self):
        self.scene(self.reject_old_key)
        with default_sigint():
            exc, out = self.attempt(KeyboardInterrupt, {}, replace_then_sigint(), self.flaky_fresh_read())
        self.assert_nothing_deleted()
        self.assertIn("unknown", out.stderr)
        self.assertNotIn("unchanged", out.stderr)
        self.assert_no_state_leak(out.text)

    def test_the_state_never_turns_into_the_manual_paste_fallback(self):
        self.scene(self.reject_old_key)
        with self.stdin(True), mock.patch.object(cak.getpass, "getpass", side_effect=AssertionError("prompted")) as gp, \
                self.refuse_readback(), self.capture(), self.assertRaises(cak.KeyStateUncertain):
            self.run_ensure(allow_prompt=True)
        gp.assert_not_called()


class NotPublishedTests(Lifecycle):
    """The write provably did not reach .env: the new key is deleted again, .env is as it was."""

    def check(self, exc, out, before):
        self.assertIsInstance(exc, cak.KeyStoreError)
        self.assertNotIsInstance(exc, cak.KeyStateUncertain)
        self.assert_untouched(*before)
        self.assertEqual([c.path for c in self.lf.calls if c.method == "DELETE"], [KEYS_PATH + NEW_ID])
        self.assertIn(".env is unchanged (compared byte for byte)", str(exc))     # the bytes were compared
        self.assertIn("removed from Langflow again", str(exc))
        self.assert_no_state_leak(str(exc) + out.text)

    def test_a_failing_rename_is_not_a_publication(self):
        for label, kwargs, argv, prepare in self.modes():
            with self.subTest(label):
                self.scene(prepare)
                before = self.snapshot()
                with mock.patch("os.replace", side_effect=OSError(errno.EACCES, f"denied {NEW_KEY}")):
                    exc, out = self.attempt(cak.KeyStoreError, kwargs)
                self.check(exc, out, before)

    def test_a_failing_temp_file_write_is_not_a_publication(self):
        for label, kwargs, argv, prepare in self.modes():
            with self.subTest(label):
                self.scene(prepare)
                before = self.snapshot()
                exc, out = self.attempt(cak.KeyStoreError, kwargs, failing_temp_write())
                self.check(exc, out, before)

    def test_the_command_exits_1_for_a_failed_write(self):
        for label, kwargs, argv, prepare in self.modes():
            with self.subTest(label):
                self.scene(prepare)
                before = self.snapshot()
                code, out = self.run_cli(argv, mock.patch("os.replace", side_effect=OSError(errno.ENOSPC, "full")))
                self.assertEqual(status_of(code), 1)
                self.assertIn(".env is unchanged (compared byte for byte)", code)
                self.assert_untouched(*before)
                self.assertEqual([c.path for c in self.lf.calls if c.method == "DELETE"], [KEYS_PATH + NEW_ID])
                self.assert_no_state_leak(code + out.text)

    def test_a_failure_before_the_write_deletes_the_new_key_by_the_reply_id(self):
        for label, kwargs, argv, prepare in self.modes():
            with self.subTest(label):
                self.scene(prepare)
                self.lf.flows_by_key[NEW_KEY] = ks.http_error(500)
                before = self.snapshot()
                exc, out = self.attempt(cak.KeyUnverifiable, kwargs)
                self.assert_untouched(*before)
                self.assertEqual([c.path for c in self.lf.calls if c.method == "DELETE"], [KEYS_PATH + NEW_ID])
                self.assertIn("It was removed again. Nothing was changed", str(exc))
                self.assert_no_state_leak(str(exc) + out.text)

    def test_a_failed_write_whose_new_key_cannot_be_identified_says_it_was_left(self):
        self.scene(self.reject_old_key, reply_id=None, before=ConnectionResetError())
        before = self.snapshot()
        with mock.patch("os.replace", side_effect=OSError(errno.EACCES, "denied")):
            exc, out = self.attempt(cak.KeyStoreError, {})
        self.assert_untouched(*before)
        self.assert_nothing_deleted()
        self.assertIn("could not be removed from Langflow and was left there", str(exc))
        self.assertIn(NOTICE_NEW_LEFT, out.stderr)
        self.assert_no_state_leak(str(exc) + out.text)


class PastedKeyTests(Lifecycle):
    """The pasted key has no Langflow-side key to delete; its messages are as truthful as the others."""

    def setUp(self):
        super().setUp()
        self.write_env()
        self.reject_old_key()
        self.lf.routes[("POST", "/api/v1/login")] = ks.http_error(401)      # the automatic path fails: ask the operator

    def paste(self, *patchers):
        with contextlib.ExitStack() as stack:
            for patcher in patchers:
                stack.enter_context(patcher)
            stack.enter_context(self.stdin(True))
            stack.enter_context(mock.patch.object(cak.getpass, "getpass", return_value=f" {PASTED_KEY} "))
            out = stack.enter_context(self.capture())
            try:
                self.run_ensure(allow_prompt=True)
                caught = None
            except BaseException as exc:                    # noqa: BLE001 - the test classifies it
                caught = exc
        return caught, out

    def assert_quiet(self, text):
        self.assertNotIn(PASTED_KEY, text)
        self.assert_no_state_leak(text)
        self.assert_nothing_deleted()

    def test_a_readback_failure_is_an_uncertain_state(self):
        exc, out = self.paste(self.refuse_readback(PASTED_KEY))
        self.assertIsInstance(exc, cak.KeyStateUncertain)
        self.assertEqual(self.key_in_env(), PASTED_KEY)
        self.assertIn("pasted key is stored in .env", str(exc))
        self.assertIn("Nothing in Langflow was touched", str(exc))
        self.assertNotIn("unchanged", str(exc))
        self.assert_quiet(str(exc) + out.text)

    def test_a_real_sigint_after_the_rename_propagates_after_one_line(self):
        with default_sigint():
            exc, out = self.paste(replace_then_sigint())
        self.assertIs(type(exc), KeyboardInterrupt)
        self.assertEqual(self.key_in_env(), PASTED_KEY)
        self.assertIn("pasted key is stored in .env", out.stderr)
        self.assertNotIn("unchanged", out.stderr)
        self.assert_quiet(out.text)

    def test_a_failing_rename_is_a_store_error_with_the_proof(self):
        before = self.snapshot()
        exc, out = self.paste(mock.patch("os.replace", side_effect=OSError(errno.EACCES, "denied")))
        self.assertIsInstance(exc, cak.KeyStoreError)
        self.assertNotIsInstance(exc, cak.KeyStateUncertain)
        self.assertIn(".env is unchanged (compared byte for byte)", str(exc))
        self.assertNotIn("removed from Langflow", str(exc))
        self.assertEqual(self.snapshot()[1], before[1])
        self.assert_quiet(str(exc) + out.text)

    def test_a_real_sigint_before_the_rename_says_unchanged_only_after_comparing(self):
        before = self.snapshot()
        with default_sigint():
            exc, out = self.paste(sigint_instead_of_replace())
        self.assertIs(type(exc), KeyboardInterrupt)
        self.assertEqual(self.snapshot(), before)
        self.assertIn(".env is unchanged (compared byte for byte)", out.stderr)
        self.assert_quiet(out.text)


class BootstrapTests(Lifecycle):
    def test_an_uncertain_state_returns_1_and_runs_nothing_more(self):
        self.scene(self.reject_old_key)
        code, up, later, out = self.run_bootstrap(self.refuse_readback())
        self.assertEqual(code, 1)
        self.assertNotEqual(code, cak.EXIT_RETRY_LATER)
        self.assertEqual(up.call_count, 1, "only the first start may run")
        for step in later:
            step.assert_not_called()
        self.assertFalse(bootstrap._key_unverifiable)
        self.assertFalse(bootstrap._needs_recreate)          # a recreate is not claimed for a key that is uncertain
        self.assertEqual(bootstrap._created_secrets, [])
        self.assertTrue(bootstrap._failed)
        self.assertNotIn("edu_ai is up", out.stdout)
        self.assertNotIn("Shown once", out.stdout)
        self.assertIn("Check LANGFLOW_API_KEY in .env", out.stdout)
        self.assertNotIn("unchanged", out.stdout)
        self.assert_nothing_deleted()
        self.assertEqual(self.key_in_env(), NEW_KEY)
        self.assert_no_state_leak(out.text)

    def test_a_write_that_provably_failed_returns_1_and_runs_nothing_more(self):
        self.scene(self.reject_old_key)
        before = self.snapshot()
        code, up, later, out = self.run_bootstrap(mock.patch("os.replace", side_effect=OSError(errno.ENOSPC, "full")))
        self.assertEqual(code, 1)
        self.assertEqual(up.call_count, 1)
        for step in later:
            step.assert_not_called()
        self.assert_untouched(*before)
        self.assertEqual([c.path for c in self.lf.calls if c.method == "DELETE"], [KEYS_PATH + NEW_ID])
        self.assertFalse(bootstrap._needs_recreate)
        self.assert_no_state_leak(out.text)


# ───────────────────────────────────────────────────────────────
#  finding 4: HTTP 403 in the automatic path
# ───────────────────────────────────────────────────────────────

class AccessDeniedTests(Lifecycle):
    def deny(self):
        self.lf.flows_by_key[OLD_KEY] = ks.http_error(403)

    def test_a_403_on_the_check_is_unverifiable_and_the_only_request(self):
        self.deny()
        before = self.snapshot()
        exc, out = self.attempt(cak.KeyUnverifiable, {})
        self.assertEqual(self.lf.paths(), [FLOWS_CHECK])                      # no login, listing, creation, deletion
        self.assert_untouched(*before)
        self.assertIn(f"({DENIED})", str(exc))
        self.assertIn("Nothing was changed", str(exc))
        self.assertIn("create_api_key.py --force", str(exc))                  # the way out for a key known to be dead
        self.assert_no_state_leak(str(exc) + out.text)

    def test_the_command_exits_3_and_changes_nothing(self):
        self.deny()
        before = self.snapshot()
        code, out = self.run_cli([])
        self.assertEqual(code, cak.EXIT_RETRY_LATER)
        self.assertEqual(self.lf.paths(), [FLOWS_CHECK])
        self.assert_untouched(*before)
        self.assertIn("NOT CHANGED", out.stderr)
        self.assertIn(DENIED, out.stderr)
        self.assert_no_state_leak(out.text)

    def test_bootstrap_returns_3_before_anything_else_starts(self):
        self.deny()
        before = self.snapshot()
        code, up, later, out = self.run_bootstrap()
        self.assertEqual(code, cak.EXIT_RETRY_LATER)
        self.assertEqual(up.call_count, 1)
        for step in later:
            step.assert_not_called()
        self.assertEqual(self.lf.paths(), [("GET", "/health"), FLOWS_CHECK])      # bootstrap probes the address first
        self.assertEqual(self.lf.mutating(), [])
        self.assert_untouched(*before)
        self.assertTrue(bootstrap._key_unverifiable)
        self.assertIn(DENIED, out.stdout)
        self.assertNotIn("edu_ai is up", out.stdout)
        self.assert_no_state_leak(out.text)

    def test_a_401_still_replaces_the_key_and_deletes_no_old_one(self):
        self.scene(self.reject_old_key)
        with self.no_prompt(tty=True), self.capture() as out:
            self.assertEqual(self.run_ensure(allow_prompt=True), (NEW_KEY, "replaced"))
        self.assertEqual(self.key_in_env(), NEW_KEY)
        self.assertEqual(self.lf.paths(), [
            FLOWS_CHECK, ("POST", "/api/v1/login"), ("GET", KEYS_PATH), ("POST", KEYS_PATH), FLOWS_CHECK])
        self.assert_nothing_deleted()
        self.assertIn("HTTP 401", out.stderr)
        self.assert_no_state_leak(out.text)

    def test_force_with_a_working_key_creates_stores_and_deletes_nothing_old(self):
        self.scene()
        with self.capture() as out:
            self.assertEqual(self.run_ensure(force=True), (NEW_KEY, "created"))
        self.assertEqual(self.key_in_env(), NEW_KEY)
        self.assertEqual(self.lf.paths(), [
            ("POST", "/api/v1/login"), ("GET", KEYS_PATH), ("POST", KEYS_PATH), FLOWS_CHECK])
        self.assertEqual(self.lf.calls[-1].headers["x-api-key"], NEW_KEY)     # only the new key is checked
        self.assert_nothing_deleted()
        self.assertIn(NOTICE_PREVIOUS, out.stderr)
        self.assert_no_state_leak(out.text)

    def test_force_replaces_a_key_that_answers_403_without_asking_it(self):
        self.scene(self.deny)
        with self.capture():
            self.assertEqual(self.run_ensure(force=True), (NEW_KEY, "created"))
        self.assertEqual(self.key_in_env(), NEW_KEY)
        self.assertTrue(all(c.headers.get("x-api-key") != OLD_KEY for c in self.lf.calls))

    def test_a_403_on_the_new_key_deletes_it_again_and_changes_nothing_else(self):
        for label, kwargs, argv, prepare in self.modes():
            with self.subTest(label):
                self.scene(prepare)
                self.lf.flows_by_key[NEW_KEY] = ks.http_error(403)
                before = self.snapshot()
                code, out = self.run_cli(argv)
                self.assertEqual(code, cak.EXIT_RETRY_LATER)
                self.assert_untouched(*before)
                self.assertEqual([c.path for c in self.lf.calls if c.method == "DELETE"], [KEYS_PATH + NEW_ID])
                self.assertEqual(sum(1 for c in self.lf.calls if c.method == "POST" and c.path == KEYS_PATH), 1)
                self.assertIn("NOT CHANGED", out.stderr)
                self.assertIn("It was removed again", out.stderr)
                self.assert_no_state_leak(out.text)

    def test_a_403_is_a_rejection_nowhere(self):
        consumers = {"bootstrap.py": ("check_key", "KEY_REJECTED"), "import_flows.py": ("check_key", "KEY_REJECTED", "create_api_key"),
                     "smoke.py": ("check_key", "KEY_REJECTED", "create_api_key")}
        for name, forbidden in consumers.items():
            source = (SCRIPTS / name).read_text(encoding="utf-8")
            for word in forbidden:
                self.assertNotIn(word, source, f"{name} depends on how create_api_key classifies a status")


# ───────────────────────────────────────────────────────────────
#  finding 5: no old key is ever deleted; a failed listing is not an empty one
# ───────────────────────────────────────────────────────────────

SHAPES = {
    "two keys with the same visible prefix": [(OLD_ID, cak.KEY_NAME, OLD_MASK), (OTHER_ID, cak.KEY_NAME, OLD_MASK)],
    "one single matching key": [(OLD_ID, cak.KEY_NAME, OLD_MASK)],
    "a key of another name with the same prefix": [(OLD_ID, "someone-elses", OLD_MASK)],
    "keys of other names and other values": [(OLD_ID, "ui-created", OTHER_MASK), (OTHER_ID, cak.KEY_NAME, OTHER_MASK)],
    "the rejected key is absent from the listing": [],
    "masked values that cannot be compared": [(OLD_ID, cak.KEY_NAME, ""), (OTHER_ID, cak.KEY_NAME, "****")],
}


class OldKeyIsKeptTests(Lifecycle):
    def test_no_old_key_is_deleted_in_any_shape_of_listing(self):
        for shape, entries in SHAPES.items():
            for label, kwargs, argv, prepare in self.modes():
                if label == "first creation":
                    continue
                with self.subTest(shape=shape, mode=label):
                    self.scene(prepare, before=entries, after=entries + [(NEW_ID, cak.KEY_NAME, NEW_MASK)])
                    with self.capture() as out:
                        self.assertEqual(self.run_ensure(**kwargs), (NEW_KEY, "created" if kwargs else "replaced"))
                    self.assert_nothing_deleted()
                    self.assertEqual(self.key_in_env(), NEW_KEY)
                    self.assertEqual(out.stderr.count(NOTICE_PREVIOUS), 1)
                    self.assert_no_state_leak(out.text)

    def test_the_ambiguous_prefix_of_the_codex_review_deletes_neither_key(self):
        self.scene(self.reject_old_key, before=SHAPES["two keys with the same visible prefix"],
                   after=SHAPES["two keys with the same visible prefix"] + [(NEW_ID, cak.KEY_NAME, NEW_MASK)])
        with self.capture():
            self.assertEqual(self.run_ensure(), (NEW_KEY, "replaced"))
        self.assert_nothing_deleted()

    def test_without_an_old_key_in_env_nothing_is_deleted_and_no_notice_is_given(self):
        for shape, entries in SHAPES.items():
            with self.subTest(shape):
                self.scene(lambda: self.write_env(LANGFLOW_API_KEY=None), before=entries,
                           after=entries + [(NEW_ID, cak.KEY_NAME, NEW_MASK)])
                with self.capture() as out:
                    self.assertEqual(self.run_ensure(), (NEW_KEY, "created"))
                self.assert_nothing_deleted()
                self.assertNotIn(NOTICE_PREVIOUS, out.stderr)
                self.assert_no_state_leak(out.text)

    def test_the_notice_names_no_value_and_no_id(self):
        self.scene(self.reject_old_key)
        with self.capture() as out:
            self.run_ensure()
        notice = [line for line in out.stderr.splitlines() if NOTICE_PREVIOUS in line]
        self.assertEqual(len(notice), 1)
        self.assertIn("Settings, API Keys", notice[0])
        self.assert_no_state_leak(notice[0])
        self.assert_no_state_leak(out.text)

    def test_the_command_prints_the_notice_after_a_replacement(self):
        self.scene(self.reject_old_key)
        code, out = self.run_cli(["--no-input"])
        self.assertIsNone(code)
        self.assertIn(NOTICE_PREVIOUS, out.stderr)
        self.assertIn("created a new one and wrote it to .env", out.stdout)
        self.assert_no_state_leak(out.text)


class OwnKeysTests(Lifecycle):
    """A failed listing is distinguishable from an empty one."""

    def listing(self, behaviour):
        self.lf.routes[("GET", KEYS_PATH)] = behaviour
        return cak.own_keys(BASE, "synthetic-bearer")

    def test_an_empty_listing_is_empty_and_a_failed_one_is_unknown(self):
        self.assertEqual(self.listing(ks.json_response({"api_keys": []})), [])
        failures = {
            "HTTP 500": ks.http_error(500),
            "HTTP 403": ks.http_error(403),
            "timeout": urllib.error.URLError(TimeoutError("t")),
            "connection reset": ConnectionResetError(),
            "malformed JSON": ks.FakeResponse(200, b'{"api_keys": ['),
            "not JSON": ks.FakeResponse(200, b"<html>sign in</html>"),
            "a list instead of an object": ks.json_response([]),
            "no api_keys member": ks.json_response({"total_count": 0}),
            "api_keys is null": ks.json_response({"api_keys": None}),
            "api_keys is a string": ks.json_response({"api_keys": "oops"}),
            "api_keys is an object": ks.json_response({"api_keys": {"id": OLD_ID}}),
            "an own key without an id": ks.json_response({"api_keys": [{"name": cak.KEY_NAME, "api_key": OLD_MASK}]}),
        }
        for label, behaviour in failures.items():
            with self.subTest(label):
                self.assertIsNone(self.listing(behaviour))

    def test_only_keys_of_this_scripts_name_are_returned(self):
        entries = [{"id": OLD_ID, "name": cak.KEY_NAME, "api_key": OLD_MASK},
                   {"id": OTHER_ID, "name": "someone-elses", "api_key": OTHER_MASK},
                   {"name": "someone-elses-without-an-id"}, "not an object"]
        self.assertEqual(self.listing(ks.json_response({"api_keys": entries})), [entries[0]])


GOOD_BEFORE = [(OLD_ID, cak.KEY_NAME, OLD_MASK)]
FAILED = ks.http_error(500)
DISCARD_CASES = {
    # label: (listing before the creation, listing after it, the id that may be deleted)
    "both listings good, exactly one fresh entry": (GOOD_BEFORE, GOOD_BEFORE + [(NEW_ID, cak.KEY_NAME, NEW_MASK)], NEW_ID),
    "several fresh entries": (GOOD_BEFORE, GOOD_BEFORE + [(NEW_ID, cak.KEY_NAME, NEW_MASK),
                                                          (EXTRA_ID, cak.KEY_NAME, NEW_MASK)], None),
    "zero fresh entries": (GOOD_BEFORE, GOOD_BEFORE, None),
    "the listing after fails (HTTP 500)": (GOOD_BEFORE, FAILED, None),
    "the listing before fails (HTTP 500)": (FAILED, GOOD_BEFORE + [(NEW_ID, cak.KEY_NAME, NEW_MASK)], None),
    "both listings fail": (FAILED, FAILED, None),
    "the listing after is malformed JSON": (GOOD_BEFORE, ks.FakeResponse(200, b'{"api_keys": ['), None),
    "the listing before times out": (urllib.error.URLError(TimeoutError("t")),
                                     GOOD_BEFORE + [(NEW_ID, cak.KEY_NAME, NEW_MASK)], None),
    "the listing after has an own entry without an id": (
        GOOD_BEFORE, ks.json_response({"api_keys": [{"name": cak.KEY_NAME, "api_key": NEW_MASK}]}), None),
}


class NewKeyDiscardTests(Lifecycle):
    """The key that was created and not stored is deleted by its id, or as the one fresh entry, or not at all."""

    def failing_check(self, reply_id, before, after):
        self.scene(self.reject_old_key, reply_id=reply_id, before=before, after=after)
        self.lf.flows_by_key[NEW_KEY] = ks.http_error(500)
        snapshot = self.snapshot()
        result = self.attempt(cak.KeyUnverifiable, {})
        self.assert_untouched(*snapshot)
        return result

    def test_a_reply_without_an_id_leaves_the_decision_to_two_good_listings(self):
        for label, (before, after, deleted) in DISCARD_CASES.items():
            with self.subTest(label):
                exc, out = self.failing_check(None, before, after)
                self.assertEqual([c.path for c in self.lf.calls if c.method == "DELETE"],
                                 [KEYS_PATH + deleted] if deleted else [])
                if deleted:
                    self.assertIn("It was removed again", str(exc))
                    self.assertNotIn("left in Langflow", out.stderr)
                else:
                    self.assertIn("could not be removed again", str(exc))
                    self.assertNotIn("removed again. Nothing", str(exc))
                    self.assertIn(NOTICE_NEW_LEFT, out.stderr)                # the operator is told the key is there
                self.assert_no_state_leak(str(exc) + out.text)

    def test_the_id_of_the_reply_is_trusted_over_any_listing(self):
        for label, (before, after, _) in DISCARD_CASES.items():
            with self.subTest(label):
                exc, out = self.failing_check(NEW_ID, before, after)
                self.assertEqual([c.path for c in self.lf.calls if c.method == "DELETE"], [KEYS_PATH + NEW_ID])
                self.assertEqual(sum(1 for c in self.lf.calls if c.method == "GET" and c.path == KEYS_PATH), 1,
                                 "the listing after the creation was consulted although the reply had an id")
                self.assertNotIn("left in Langflow", out.stderr)
                self.assert_no_state_leak(str(exc) + out.text)

    def test_a_listing_that_fails_never_looks_like_an_empty_one(self):
        # The old reading of "empty": every entry of a good listing after the creation would be "fresh".
        exc, out = self.failing_check(None, FAILED, [(NEW_ID, cak.KEY_NAME, NEW_MASK)])
        self.assertEqual([c for c in self.lf.calls if c.method == "DELETE"], [])
        self.assertIn(NOTICE_NEW_LEFT, out.stderr)

    def test_a_delete_that_fails_is_reported_and_is_not_fatal(self):
        self.lf.delete_behavior = ks.http_error(500)
        exc, out = self.failing_check(NEW_ID, GOOD_BEFORE, GOOD_BEFORE)
        self.assertIn("could not be removed again", str(exc))
        self.assertIn(NOTICE_NEW_LEFT, out.stderr)
        self.assert_no_state_leak(str(exc) + out.text)

    def test_an_interrupt_during_the_check_deletes_the_new_key_and_propagates(self):
        self.scene(self.reject_old_key)
        before = self.snapshot()
        with mock.patch.object(cak, "check_key", side_effect=[cak.KeyCheck(cak.KEY_REJECTED, "HTTP 401"),
                                                              KeyboardInterrupt()]):
            with self.capture() as out, self.assertRaises(KeyboardInterrupt):
                self.run_ensure()
        self.assert_untouched(*before)
        self.assertEqual([c.path for c in self.lf.calls if c.method == "DELETE"], [KEYS_PATH + NEW_ID])
        self.assert_no_state_leak(out.text)


if __name__ == "__main__":
    unittest.main()
