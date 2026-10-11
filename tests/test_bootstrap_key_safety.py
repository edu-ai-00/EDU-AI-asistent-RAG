"""B06 bootstrap hardening: Langflow API key safety and secrets-free output.

Run: python tests/test_bootstrap_key_safety.py   (add -v for names)

Offline by construction: a synthetic .env in a temporary directory, HTTP answered by
a fake that replaces urllib.request.urlopen, and guards that make any subprocess,
socket or access to the repository's real .env fail the test. No Docker, no
Langflow, no network. Standard library only.

What it pins down
  * a key in .env is valid, rejected (401 only) or unverifiable (a 403 included), and only a
    rejection (or no key at all) may lead to a new key; an unverifiable state leaves .env
    byte-for-byte unchanged, sends no create/delete request and offers no prompt;
  * nothing printed (stdout or stderr, error paths included) carries a synthetic
    password, key or response body;
  * the closing overview shows the local secrets THIS run created, once, only at an
    interactive terminal; never existing, external or pasted values; never in CI or
    redirected output, where it gives the path to .env only.
"""
import ast
import contextlib
import errno
import http.client
import io
import json
import os
import pathlib
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from collections import namedtuple
from unittest import mock

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import bootstrap  # noqa: E402
import create_api_key as cak  # noqa: E402
import http_safe  # noqa: E402
from envfile import read_env  # noqa: E402

REAL_ENV = (SCRIPTS.parent / ".env").absolute()
BASE = "http://langflow.test:7860"

# Synthetic values only. Never real credentials.
PG_PASSWORD = "syn-pg-pass-0f3a9c1b7d"
PGADMIN_PASSWORD = "syn-pgadmin-pass-4e8d2a6c"
ROLE_PASSWORDS = {"POSTGRES_ADMIN_PASSWORD": "syn-role-admin-1a2b3c4d5e", "POSTGRES_SEARCH_PASSWORD": "syn-role-search-2b3c4d5e6f",
                  "POSTGRES_LANGFLOW_PASSWORD": "syn-role-langflow-3c4d5e6f7a", "POSTGRES_MIGRATOR_PASSWORD": "syn-role-migrator-4d5e6f7a8b"}
SU_PASSWORD = "syn-su-pass-9b1c5d7e"
FERNET = "c3luLWZlcm5ldC1rZXktMDEyMzQ1Njc4OWFiY2RlZmdoaWo="
OLD_KEY = "sk-syn-old-key-aaaaaaaaaaaaaaaa"
OPENAI_KEY = "sk-syn-openai-bbbbbbbbbbbbbbbb"
CF_TOKEN = "syn-cf-token-cccccccccccccccc"
NEW_KEY = "sk-syn-new-key-dddddddddddddddd"
PASTED_KEY = "sk-syn-pasted-eeeeeeeeeeeeeeee"
BODY_SECRET = "syn-body-secret-ffffffff"
BEARER = "syn-bearer-token-gggggggg"
ALL_SECRETS = [PG_PASSWORD, PGADMIN_PASSWORD, SU_PASSWORD, FERNET, OLD_KEY, OPENAI_KEY,
               CF_TOKEN, NEW_KEY, PASTED_KEY, BODY_SECRET, BEARER, *ROLE_PASSWORDS.values()]

HOSTILE_BODY = json.dumps(
    {"detail": f"echo {BODY_SECRET} key={OLD_KEY} password={SU_PASSWORD}"}).encode()

CREATED_NAMES = ["POSTGRES_PASSWORD", "PGADMIN_DEFAULT_PASSWORD",
                 "LANGFLOW_SUPERUSER_PASSWORD", "LANGFLOW_SECRET_KEY", *ROLE_PASSWORDS]


# ───────────────────────────────────────────────────────────────
#  Fixtures
# ───────────────────────────────────────────────────────────────

def synthetic_env(**override) -> bytes:
    """A .env with mixed line endings and no final newline, so any rewrite by
    set_env (LF only, trailing newline) changes the bytes. A value of None omits
    the line."""
    values = {
        "POSTGRES_PASSWORD": PG_PASSWORD, "PGADMIN_DEFAULT_PASSWORD": PGADMIN_PASSWORD,
        "LANGFLOW_SUPERUSER_PASSWORD": SU_PASSWORD, "LANGFLOW_SECRET_KEY": FERNET,
        "LANGFLOW_API_KEY": OLD_KEY, "OPENAI_API_KEY": OPENAI_KEY,
        "CLOUDFLARE_TUNNEL_TOKEN": CF_TOKEN, **ROLE_PASSWORDS,
    }
    values.update(override)
    lines = [
        "# synthetic test environment - not a real configuration",
        "COMPOSE_PROJECT_NAME=edu_ai",
        "export LANGFLOW_SUPERUSER=admin",
        "PGADMIN_DEFAULT_EMAIL=admin@example.com",
        "POSTGRES_DB_SERVER=postgres", "POSTGRES_SERVER_PORT=5432", "POSTGRES_DB=edu",
        "POSTGRES_DB_SCHEMA=edu_ai", "POSTGRES_DB_USER=edu",
        "",
    ]
    lines += [f"{name}={value}" for name, value in values.items() if value is not None]
    lines.append("# trailing comment, no newline after it")
    head = "\r\n".join(lines[:4]) + "\r\n"
    return (head + "\n".join(lines[4:])).encode()


SYNTHETIC_EXAMPLE = f"""# synthetic .env.example
COMPOSE_PROJECT_NAME=edu_ai
PGADMIN_DEFAULT_EMAIL=admin@example.com
POSTGRES_DB_SERVER=postgres
POSTGRES_SERVER_PORT=5432
POSTGRES_DB=edu
POSTGRES_DB_SCHEMA=edu_ai
POSTGRES_DB_USER=edu
POSTGRES_PASSWORD=CHANGE_ME_postgres_password
PGADMIN_DEFAULT_PASSWORD=CHANGE_ME_pgadmin_password
LANGFLOW_API_KEY=CHANGE_ME_langflow_api_key
LANGFLOW_SUPERUSER=admin
LANGFLOW_SUPERUSER_PASSWORD=CHANGE_ME_langflow_admin_password
LANGFLOW_SECRET_KEY=CHANGE_ME_langflow_secret_key
POSTGRES_ADMIN_PASSWORD=CHANGE_ME_postgres_admin_password
POSTGRES_SEARCH_PASSWORD=CHANGE_ME_postgres_search_password
POSTGRES_LANGFLOW_PASSWORD=CHANGE_ME_postgres_langflow_password
POSTGRES_MIGRATOR_PASSWORD=CHANGE_ME_postgres_migrator_password
OPENAI_API_KEY={OPENAI_KEY}
CLOUDFLARE_TUNNEL_TOKEN={CF_TOKEN}
"""

Call = namedtuple("Call", "method path headers body")


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None, read_error=None):
        self.status = status
        self.headers = headers or {}
        self._body = body
        self._read_error = read_error

    def read(self):
        if self._read_error:
            raise self._read_error
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def json_response(obj, status=200):
    return FakeResponse(status, json.dumps(obj).encode())


def http_error(code, body=HOSTILE_BODY):
    # The reason phrase carries a secret on purpose: str(exc) must never be printed.
    return urllib.error.HTTPError(f"{BASE}/x", code, f"reason {OLD_KEY}", {}, io.BytesIO(body))


class FakeLangflow:
    """Replaces urllib.request.urlopen; records every request.

    Each route holds a FakeResponse, an exception to raise, or a callable.
    """

    def __init__(self):
        self.calls = []
        self.flows_by_key = {}        # x-api-key -> behaviour of GET /api/v1/flows/ for that key alone
        self.delete_behavior = None   # replaces the answer to DELETE /api/v1/api_key/<id>
        self.on_call = None           # called with each Call just before it is answered
        self.routes = {
            ("GET", "/health"): json_response({"status": "ok"}),
            ("GET", "/api/v1/flows/"): json_response({"items": [], "total": 0}),
            ("POST", "/api/v1/login"): json_response({"access_token": BEARER}),
            ("GET", "/api/v1/api_key/"): json_response({"api_keys": [
                {"id": "k1", "name": cak.KEY_NAME, "api_key": OLD_KEY[:8] + "****"},
                {"id": "k2", "name": "someone-elses", "api_key": "sk-els-e****"}]}),
            ("POST", "/api/v1/api_key/"): json_response({"api_key": NEW_KEY}),
            ("GET", "/api/v1/variables/"): json_response([]),
            ("POST", "/api/v1/variables/"): json_response({}),
        }

    def __call__(self, target, data=None, timeout=None):
        if isinstance(target, str):
            method, url, headers, body = "GET", target, {}, data
        else:
            method, url, body = target.get_method(), target.full_url, target.data
            headers = {k.lower(): v for k, v in target.header_items()}
        parts = urllib.parse.urlsplit(url)
        self.calls.append(Call(method, parts.path + (f"?{parts.query}" if parts.query else ""),
                               headers, body))
        key = (method, parts.path)
        if self.on_call:
            self.on_call(self.calls[-1])
        if method == "DELETE" and parts.path.startswith("/api/v1/api_key/"):
            behavior = self.delete_behavior or json_response({"detail": "deleted"})
        elif key == ("GET", "/api/v1/flows/") and headers.get("x-api-key") in self.flows_by_key:
            behavior = self.flows_by_key[headers["x-api-key"]]
        elif key in self.routes:
            behavior = self.routes[key]
        else:
            raise AssertionError(f"unexpected request {method} {parts.path}")
        if isinstance(behavior, BaseException):
            raise behavior
        return behavior() if callable(behavior) else behavior

    def mutating(self):
        return [c for c in self.calls if c.method != "GET"]

    def paths(self):
        return [(c.method, c.path) for c in self.calls]


def unverifiable_cases():
    """(label, what the flows endpoint does, expected detail). Every one of them
    says nothing about whether the key is good."""
    cases = []
    for code in (400, 404, 422, 429, 500, 502, 503, 504):
        cases.append((f"HTTP {code}", http_error(code), f"HTTP {code}"))
    cases += [
        ("HTTP 403, a refusal is not a rejection", http_error(403),
         "HTTP 403, access denied: not proof that the key is invalid"),
        ("redirect not followed", http_error(302),
         "HTTP 302 redirect, not followed; set LANGFLOW_URL to the final address"),
        ("timeout while connecting", urllib.error.URLError(TimeoutError("timed out")), "timeout"),
        ("bare timeout", TimeoutError(), "timeout"),
        ("socket timeout", socket.timeout(), "timeout"),
        ("timeout while reading", FakeResponse(read_error=TimeoutError()), "timeout"),
        ("connection refused",
         urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, "refused")),
         "connection error"),
        ("connection reset", ConnectionResetError(), "connection error"),
        ("name not resolved", urllib.error.URLError(socket.gaierror(-2, "unknown host")),
         "connection error"),
        ("tls failure", urllib.error.URLError(ssl.SSLError(f"bad certificate {OLD_KEY}")),
         "connection error"),
        ("remote closed", http.client.RemoteDisconnected("closed"), "connection error"),
        ("incomplete read", FakeResponse(read_error=http.client.IncompleteRead(b"par", 10)),
         "connection error"),
        ("error text with a secret", urllib.error.URLError(f"cannot reach {OLD_KEY}"),
         "connection error"),
        ("201 instead of 200", FakeResponse(201, b"{}"), "unexpected HTTP 201"),
        ("204 no content", FakeResponse(204, b""), "unexpected HTTP 204"),
        ("200 empty body", FakeResponse(200, b""), "malformed response"),
        ("200 html page", FakeResponse(200, b"<html>sign in</html>"), "malformed response"),
        ("200 truncated json", FakeResponse(200, b'{"items": ['), "malformed response"),
        ("200 json null", FakeResponse(200, b"null"), "malformed response"),
        ("200 json string", FakeResponse(200, b'"ok"'), "malformed response"),
        ("200 json number", FakeResponse(200, b"7"), "malformed response"),
        ("200 bad bytes", FakeResponse(200, b"\xff\xfe\x00\x80"), "malformed response"),
        ("200 corrupt gzip", FakeResponse(200, b"not gzip", {"Content-Encoding": "gzip"}),
         "malformed response"),
        ("unexpected error type", RuntimeError(f"boom {OLD_KEY}"), "unexpected error"),
        ("value error with a secret", ValueError(f"bad {OLD_KEY}"), "malformed response"),
    ]
    return cases


class TTYBuffer(io.StringIO):
    def isatty(self):
        return True


class FakeStdin:
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


class Captured:
    def __init__(self, out, err):
        self._out, self._err = out, err

    stdout = property(lambda self: self._out.getvalue())
    stderr = property(lambda self: self._err.getvalue())
    text = property(lambda self: self.stdout + "\n" + self.stderr)


class Offline(unittest.TestCase):
    """Every test runs with HTTP faked, the real .env and every other I/O refused."""

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)
        self.env_path = self.tmp / ".env"
        self.example_path = self.tmp / ".env.example"
        self.env_path.write_bytes(synthetic_env())

        self.lf = FakeLangflow()
        stack.enter_context(mock.patch("urllib.request.urlopen", self.lf))      # unauthenticated probes
        stack.enter_context(mock.patch.object(http_safe.OPENER, "open", self.lf))   # every call that carries a secret
        for module in (bootstrap, cak):
            stack.enter_context(mock.patch.object(module, "ENV_PATH", self.env_path))
        stack.enter_context(mock.patch.object(bootstrap, "ENV_EXAMPLE", self.example_path))

        def refuse(what):
            return mock.patch(what, side_effect=AssertionError(f"offline test used {what}"))
        for what in ("subprocess.run", "subprocess.Popen", "socket.create_connection"):
            stack.enter_context(refuse(what))
        stack.enter_context(mock.patch.object(
            socket.socket, "connect", side_effect=AssertionError("offline test opened a socket")))
        for name in ("is_file", "exists", "read_text", "read_bytes", "write_text", "write_bytes", "open"):
            stack.enter_context(self._guard_real_env(name))

        stack.enter_context(mock.patch.dict(os.environ))
        for name in bootstrap._CI_VARS:
            os.environ.pop(name, None)

        self.reset_state()
        self.addCleanup(self.reset_state)

    @staticmethod
    def _guard_real_env(name):
        original = getattr(pathlib.Path, name)

        def guarded(self, *args, **kwargs):
            if self.absolute() == REAL_ENV:
                raise AssertionError(f"test touched the repository's real .env ({name})")
            return original(self, *args, **kwargs)
        return mock.patch.object(pathlib.Path, name, guarded)

    @staticmethod
    def reset_state():
        bootstrap._summary.clear()
        bootstrap._created_secrets.clear()
        bootstrap._runtime_secrets.clear()
        bootstrap._last_env_secrets.clear()
        bootstrap._failed = False
        bootstrap._needs_recreate = False
        bootstrap._key_unverifiable = False

    # helpers ------------------------------------------------------------
    def write_env(self, **override):
        self.env_path.write_bytes(synthetic_env(**override))

    def env_state(self):
        return self.env_path.read_bytes(), self.env_path.stat().st_mtime_ns

    @contextlib.contextmanager
    def capture(self, tty=False):
        out = TTYBuffer() if tty else io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            yield Captured(out, err)

    @contextlib.contextmanager
    def stdin(self, tty):
        with mock.patch.object(sys, "stdin", FakeStdin(tty)):
            yield

    @contextlib.contextmanager
    def no_prompt(self, tty=True):
        """A terminal on stdin, with every way of asking the operator for a key armed."""
        with self.stdin(tty), \
                mock.patch.object(cak.getpass, "getpass", side_effect=AssertionError("prompted")) as gp, \
                mock.patch("builtins.input", side_effect=AssertionError("prompted")):
            yield gp

    def assert_clean(self, text, allowed=()):
        for secret in ALL_SECRETS:
            if secret in allowed:
                continue
            self.assertNotIn(secret, text)
            if len(secret) >= 12:
                self.assertNotIn(secret[:8], text, f"prefix of a secret leaked: {secret[:8]}")


# ───────────────────────────────────────────────────────────────
#  check_key: three answers
# ───────────────────────────────────────────────────────────────

class CheckKeyTests(Offline):
    def test_valid_key_is_one_get_with_the_key_in_the_header(self):
        for payload in ({"items": [], "total": 0}, [], {"detail": "x"}):
            with self.subTest(payload=payload):
                self.lf.calls.clear()
                self.lf.routes[("GET", "/api/v1/flows/")] = json_response(payload)
                check = cak.check_key(BASE, OLD_KEY)
                self.assertEqual(check.status, cak.KEY_VALID)
                self.assertEqual(self.lf.paths(), [("GET", "/api/v1/flows/?page=1&size=1")])
                self.assertEqual(self.lf.calls[0].headers["x-api-key"], OLD_KEY)

    def test_only_401_rejects_and_a_403_is_unverifiable(self):
        self.lf.routes[("GET", "/api/v1/flows/")] = http_error(401)
        check = cak.check_key(BASE, OLD_KEY)
        self.assertEqual((check.status, check.detail), (cak.KEY_REJECTED, "HTTP 401"))
        self.lf.routes[("GET", "/api/v1/flows/")] = http_error(403)
        check = cak.check_key(BASE, OLD_KEY)
        self.assertEqual((check.status, check.detail),
                         (cak.KEY_UNVERIFIABLE, "HTTP 403, access denied: not proof that the key is invalid"))
        self.assert_clean(check.detail)

    def test_everything_else_is_unverifiable_with_a_fixed_detail(self):
        for label, behavior, detail in unverifiable_cases():
            with self.subTest(label):
                self.lf.routes[("GET", "/api/v1/flows/")] = behavior
                check = cak.check_key(BASE, OLD_KEY)
                self.assertEqual((check.status, check.detail), (cak.KEY_UNVERIFIABLE, detail))
                self.assert_clean(check.detail)

    def test_no_key_is_absent_and_asks_nobody(self):
        for value in (None, "", "   ", "CHANGE_ME_langflow_api_key"):
            with self.subTest(value=value):
                self.assertEqual(cak.check_key(BASE, value).status, cak.KEY_ABSENT)
        self.assertEqual(self.lf.calls, [])

    def test_describe_error_never_repeats_exception_text(self):
        for exc in (urllib.error.URLError(OLD_KEY), ValueError(OLD_KEY), OSError(OLD_KEY),
                    http_error(500), RuntimeError(OLD_KEY), KeyError(OLD_KEY)):
            self.assertNotIn(OLD_KEY, cak.describe_error(exc))

    def test_display_url_drops_credentials_query_and_fragment(self):
        self.assertEqual(cak.display_url(f"http://admin:{BODY_SECRET}@host:7860/p?token={OLD_KEY}#f"),
                         "http://host:7860/p")
        self.assertEqual(cak.display_url(BASE), BASE)


# ───────────────────────────────────────────────────────────────
#  ensure_api_key
# ───────────────────────────────────────────────────────────────

class EnsureApiKeyTests(Offline):
    def ensure(self, **kwargs):
        kwargs.setdefault("allow_prompt", False)
        return cak.ensure_api_key(BASE, self.env_path, **kwargs)

    def other_settings(self):
        env = read_env(self.env_path)
        env.pop("LANGFLOW_API_KEY", None)
        return env

    def test_valid_key_changes_nothing(self):
        before, settings = self.env_state(), self.other_settings()
        with self.capture() as out:
            result = self.ensure()
        self.assertEqual(result, (OLD_KEY, "skip"))
        self.assertEqual(self.env_state(), before)
        self.assertEqual(self.lf.paths(), [("GET", "/api/v1/flows/?page=1&size=1")])
        self.assertEqual(self.other_settings(), settings)
        self.assert_clean(out.text)

    def test_first_creation_is_possible_without_a_key(self):
        variants = {"no line": None, "empty value": "", "placeholder": "CHANGE_ME_langflow_api_key"}
        for label, value in variants.items():
            with self.subTest(label):
                self.lf.calls.clear()
                self.write_env(LANGFLOW_API_KEY=value)
                settings = self.other_settings()
                with self.capture() as out:
                    result = self.ensure()
                self.assertEqual(result, (NEW_KEY, "created"))
                self.assertEqual(read_env(self.env_path)["LANGFLOW_API_KEY"], NEW_KEY)
                self.assertEqual(self.other_settings(), settings)
                self.assertEqual(self.lf.paths(), [
                    ("POST", "/api/v1/login"), ("GET", "/api/v1/api_key/"),
                    ("POST", "/api/v1/api_key/"), ("GET", "/api/v1/flows/?page=1&size=1")])   # no old key: nothing retired
                self.assert_clean(out.text)

    def test_rejected_key_is_replaced_without_force(self):
        self.write_env()
        self.lf.flows_by_key[OLD_KEY] = http_error(401)
        with self.capture() as out:
            result = self.ensure()
        self.assertEqual(result, (NEW_KEY, "replaced"))
        self.assertEqual(read_env(self.env_path)["LANGFLOW_API_KEY"], NEW_KEY)
        self.assertEqual(self.lf.paths(), [      # the previous key is never deleted
            ("GET", "/api/v1/flows/?page=1&size=1"), ("POST", "/api/v1/login"),
            ("GET", "/api/v1/api_key/"), ("POST", "/api/v1/api_key/"),
            ("GET", "/api/v1/flows/?page=1&size=1")])
        self.assertIn("HTTP 401", out.stderr)
        self.assert_clean(out.text)

    def test_unverifiable_key_changes_nothing_and_asks_nobody(self):
        for label, behavior, _ in unverifiable_cases():
            with self.subTest(label):
                self.lf.calls.clear()
                self.write_env()
                self.lf.routes[("GET", "/api/v1/flows/")] = behavior
                before = self.env_state()
                with self.no_prompt(tty=True) as prompt, self.capture() as out:
                    with self.assertRaises(cak.KeyUnverifiable) as caught:
                        self.ensure(allow_prompt=True)
                self.assertEqual(self.env_state(), before)
                self.assertEqual(self.lf.mutating(), [])
                self.assertEqual(self.lf.paths(), [("GET", "/api/v1/flows/?page=1&size=1")])
                prompt.assert_not_called()
                self.assertIsInstance(caught.exception, RuntimeError)
                self.assertIn("Nothing was changed", str(caught.exception))
                self.assert_clean(str(caught.exception) + out.text)

    def test_force_is_still_the_operators_manual_override(self):
        self.write_env()
        with self.capture():
            result = self.ensure(force=True)
        self.assertEqual(result, (NEW_KEY, "created"))
        self.assertTrue(all(c.headers.get("x-api-key") != OLD_KEY for c in self.lf.calls),
                        "force must not check the old key")
        self.assertEqual(read_env(self.env_path)["LANGFLOW_API_KEY"], NEW_KEY)

    def test_rejected_key_whose_replacement_fails_is_left_in_place(self):
        failures = {
            "login 401": ("POST", "/api/v1/login", http_error(401)),
            "login timeout": ("POST", "/api/v1/login", urllib.error.URLError(TimeoutError("t"))),
            "login without token": ("POST", "/api/v1/login", json_response({"unexpected": 1})),
            "login list reply": ("POST", "/api/v1/login", json_response([1, 2])),
            "create 500": ("POST", "/api/v1/api_key/", http_error(500)),
            "create without key": ("POST", "/api/v1/api_key/", json_response({"x": 1})),
        }
        for label, (method, path, behavior) in failures.items():
            with self.subTest(label):
                self.lf.calls.clear()
                self.write_env()
                self.lf.routes[("GET", "/api/v1/flows/")] = http_error(401)
                self.lf.routes[(method, path)] = behavior
                before = self.env_state()
                with self.stdin(False), self.capture() as out:
                    with self.assertRaises(RuntimeError) as caught:
                        self.ensure(allow_prompt=True)
                self.assertNotIsInstance(caught.exception, cak.KeyUnverifiable)
                self.assertEqual(self.env_state(), before)
                self.assert_clean(str(caught.exception) + out.text)

    def test_login_failure_stops_before_any_key_is_removed_or_created(self):
        self.lf.routes[("GET", "/api/v1/flows/")] = http_error(401)
        self.lf.routes[("POST", "/api/v1/login")] = http_error(401)
        with self.stdin(False), self.capture(), self.assertRaises(RuntimeError):
            self.ensure(allow_prompt=True)
        self.assertEqual([c for c in self.lf.paths() if c[1].startswith("/api/v1/api_key")], [])

    def test_manual_paste_is_offered_only_for_an_absent_or_rejected_key(self):
        for label, env, check in (
                ("absent", {"LANGFLOW_API_KEY": "CHANGE_ME_langflow_api_key"}, None),
                ("rejected", {}, http_error(401))):
            with self.subTest(label):
                self.lf.calls.clear()
                self.write_env(**env)
                if check:
                    self.lf.flows_by_key[OLD_KEY] = check
                self.lf.routes[("POST", "/api/v1/login")] = http_error(401)
                with self.stdin(True), \
                        mock.patch.object(cak.getpass, "getpass", return_value=f" {PASTED_KEY} ") as gp, \
                        self.capture() as out:
                    result = self.ensure(allow_prompt=True)
                self.assertEqual(result, (PASTED_KEY, "manual"))
                self.assertEqual(read_env(self.env_path)["LANGFLOW_API_KEY"], PASTED_KEY)
                gp.assert_called_once()
                self.assert_clean(out.text, allowed=(PASTED_KEY,))
                self.assertNotIn(PASTED_KEY, out.text)

    def test_no_prompt_without_a_terminal_or_with_no_input(self):
        self.lf.routes[("GET", "/api/v1/flows/")] = http_error(401)
        self.lf.routes[("POST", "/api/v1/login")] = http_error(401)
        for tty, allow in ((False, True), (True, False)):
            with self.subTest(tty=tty, allow_prompt=allow):
                before = self.env_state()
                with self.no_prompt(tty=tty) as prompt, self.capture() as out:
                    with self.assertRaises(RuntimeError) as caught:
                        self.ensure(allow_prompt=allow)
                prompt.assert_not_called()
                self.assertEqual(self.env_state(), before)
                self.assert_clean(str(caught.exception) + out.text)

    def test_a_write_failure_after_creation_is_reported_without_the_key(self):
        self.write_env(LANGFLOW_API_KEY=None)
        with mock.patch.object(cak, "set_env", side_effect=PermissionError(13, f"denied {NEW_KEY}")):
            with self.capture() as out, self.assertRaises(cak.KeyServiceError) as caught:
                self.ensure()
        self.assert_clean(str(caught.exception) + out.text)


# ───────────────────────────────────────────────────────────────
#  create_api_key.py as a command
# ───────────────────────────────────────────────────────────────

class CommandLineTests(Offline):
    def run_main(self, *argv):
        code = None
        with mock.patch.object(sys, "argv", ["create_api_key.py", "--url", BASE, *argv]), \
                self.capture() as out:
            try:
                cak.main()
            except SystemExit as exc:
                code = exc.code
        return code, out

    def test_valid_key(self):
        code, out = self.run_main("--no-input")
        self.assertIsNone(code)
        self.assertIn("already works", out.stdout)

    def test_unverifiable_exits_3_and_changes_nothing(self):
        self.lf.routes[("GET", "/api/v1/flows/")] = http_error(503)
        before = self.env_state()
        with self.no_prompt(tty=True) as prompt:
            code, out = self.run_main()
        self.assertEqual(code, cak.EXIT_RETRY_LATER)
        self.assertEqual(code, 3)
        self.assertEqual(self.env_state(), before)
        self.assertEqual(self.lf.mutating(), [])
        prompt.assert_not_called()
        self.assertIn("NOT CHANGED", out.stderr)
        self.assert_clean(out.text)

    def test_created_and_replaced_do_not_print_any_part_of_the_key(self):
        for label, check, word in (("created", None, "Created a new API key"),
                                   ("replaced", http_error(401), "rejected")):
            with self.subTest(label):
                self.write_env(**({"LANGFLOW_API_KEY": None} if check is None else {}))
                if check:
                    self.lf.flows_by_key[OLD_KEY] = check
                code, out = self.run_main("--no-input")
                self.assertIsNone(code)
                self.assertIn(word, out.stdout + out.stderr)
                self.assertEqual(read_env(self.env_path)["LANGFLOW_API_KEY"], NEW_KEY)
                self.assert_clean(out.text)

    def test_failure_is_an_error_not_a_retry(self):
        self.write_env(LANGFLOW_API_KEY=None)
        self.lf.routes[("POST", "/api/v1/login")] = http_error(401)
        code, out = self.run_main("--no-input")
        self.assertIsInstance(code, str)
        self.assertTrue(code.startswith("ERROR:"))
        self.assert_clean(str(code) + out.text)

    def test_force_flag_still_mints_a_key(self):
        code, out = self.run_main("--force", "--no-input")
        self.assertIsNone(code)
        self.assertEqual(read_env(self.env_path)["LANGFLOW_API_KEY"], NEW_KEY)
        self.assert_clean(out.text)


# ───────────────────────────────────────────────────────────────
#  bootstrap steps
# ───────────────────────────────────────────────────────────────

class RedactionTests(Offline):
    def test_masks_env_secrets_runtime_secrets_and_leaves_the_rest(self):
        bootstrap._runtime_secrets.add(PASTED_KEY)
        text = (f"a {PG_PASSWORD} b {FERNET} c {OLD_KEY} d {PASTED_KEY} e {OPENAI_KEY} "
                f"f {CF_TOKEN} admin@example.com edu_ai")
        masked = bootstrap.redact(text)
        for secret in (PG_PASSWORD, FERNET, OLD_KEY, PASTED_KEY, OPENAI_KEY, CF_TOKEN):
            self.assertNotIn(secret, masked)
        self.assertIn("admin@example.com edu_ai", masked)

    def test_short_and_placeholder_values_are_not_masked(self):
        self.write_env(POSTGRES_PASSWORD="abc", LANGFLOW_API_KEY="CHANGE_ME_langflow_api_key")
        self.assertEqual(bootstrap.redact("abc CHANGE_ME_langflow_api_key"),
                         "abc CHANGE_ME_langflow_api_key")

    def test_report_fail_and_relay_all_redact(self):
        with self.capture() as out:
            bootstrap.report("ok", f"x {PG_PASSWORD}")
            bootstrap.fail(f"y {SU_PASSWORD}", f"hint {FERNET}")
            bootstrap.relay(f"  [pass] z {OLD_KEY}\n\n  [FAIL] boom: {BODY_SECRET}\n")
        self.assert_clean(out.text)
        self.assertIn("[redacted]", out.stdout)
        self.assertIn("(details withheld)", out.stdout)
        self.assertTrue(bootstrap._failed)

    def test_relay_redacts_before_shortening(self):
        line = "  [pass] " + "x" * 140 + PG_PASSWORD
        with self.capture() as out:
            bootstrap.relay(line)
        self.assertNotIn(PG_PASSWORD[:10], out.stdout)


class ApiKeyStepTests(Offline):
    def step(self, url=BASE, allow_prompt=False):
        return bootstrap.step_api_key(url, allow_prompt)

    def test_valid_key_is_passed_on_and_nothing_is_recreated(self):
        before = self.env_state()
        with self.capture() as out:
            key = self.step()
        self.assertEqual(key, OLD_KEY)
        self.assertFalse(bootstrap._needs_recreate)
        self.assertEqual(bootstrap._created_secrets, [])
        self.assertEqual(self.env_state(), before)
        self.assert_clean(out.text)

    def test_unverifiable_key_stops_the_step_and_changes_nothing(self):
        for label, behavior, _ in unverifiable_cases():
            with self.subTest(label):
                self.reset_state()
                self.lf.calls.clear()
                self.write_env()
                self.lf.routes[("GET", "/api/v1/flows/")] = behavior
                before = self.env_state()
                with self.no_prompt(tty=True) as prompt, self.capture() as out:
                    key = self.step(allow_prompt=True)
                self.assertIsNone(key)
                self.assertTrue(bootstrap._failed)
                self.assertTrue(bootstrap._key_unverifiable)
                self.assertFalse(bootstrap._needs_recreate)
                self.assertEqual(bootstrap._created_secrets, [])
                self.assertEqual(self.env_state(), before)
                self.assertEqual(self.lf.mutating(), [])
                prompt.assert_not_called()
                self.assertIn("re-run", out.stdout)
                self.assertIn("Nothing was changed", out.stdout)
                self.assert_clean(out.text)

    def test_first_creation_and_replacement_are_recorded_as_created_now(self):
        for label, check, outcome_text in (
                ("created", None, "created a new API key"),
                ("replaced", http_error(401), "rejected")):
            with self.subTest(label):
                self.reset_state()
                self.write_env(**({"LANGFLOW_API_KEY": None} if check is None else {}))
                if check:
                    self.lf.flows_by_key[OLD_KEY] = check
                with self.capture() as out:
                    key = self.step()
                self.assertEqual(key, NEW_KEY)
                self.assertTrue(bootstrap._needs_recreate)
                self.assertEqual(bootstrap._created_secrets, ["LANGFLOW_API_KEY"])
                self.assertIn(outcome_text, out.stdout)
                self.assert_clean(out.text)

    def test_a_pasted_key_is_not_something_this_run_created(self):
        self.write_env(LANGFLOW_API_KEY=None)
        self.lf.routes[("POST", "/api/v1/login")] = http_error(401)
        with self.stdin(True), mock.patch.object(cak.getpass, "getpass", return_value=PASTED_KEY), \
                self.capture() as out:
            key = self.step(allow_prompt=True)
        self.assertEqual(key, PASTED_KEY)
        self.assertEqual(bootstrap._created_secrets, [])
        self.assert_clean(out.text, allowed=(PASTED_KEY,))
        self.assertNotIn(PASTED_KEY, out.text)

    def test_failures_print_no_response_body(self):
        self.write_env(LANGFLOW_API_KEY=None)
        self.lf.routes[("POST", "/api/v1/login")] = http_error(401)
        with self.stdin(False), self.capture() as out:
            self.assertIsNone(self.step(allow_prompt=True))
        self.assertTrue(bootstrap._failed)
        self.assertFalse(bootstrap._key_unverifiable)
        self.assert_clean(out.text)

    def test_url_credentials_are_not_echoed(self):
        with self.capture() as out:
            self.step(url=f"http://admin:{BODY_SECRET}@langflow.test:7860")
        self.assertNotIn(BODY_SECRET, out.text)

    def test_the_automatic_path_never_passes_force(self):
        seen = {}

        def fake(base_url, env_path=None, **kwargs):
            seen.update(kwargs)
            return OLD_KEY, "skip"
        with mock.patch.object(bootstrap, "ensure_api_key", side_effect=fake), self.capture():
            self.step()
        self.assertNotIn("force", seen)
        tree = ast.parse((SCRIPTS / "bootstrap.py").read_text(encoding="utf-8"))
        forced = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.keyword) and n.arg == "force"]
        self.assertEqual(forced, [])


class LaterStepTests(Offline):
    def test_variables_get_the_key_and_failures_show_no_body(self):
        self.lf.routes[("GET", "/api/v1/variables/")] = json_response([{"name": "POSTGRES_DB"}])
        with self.capture() as out:
            bootstrap.step_langflow_vars(BASE, NEW_KEY)
        posts = [c for c in self.lf.calls if c.method == "POST"]
        self.assertEqual(len(posts), 4)
        self.assertTrue(all(c.headers["x-api-key"] == NEW_KEY for c in self.lf.calls))
        self.assertFalse(bootstrap._failed)
        self.assert_clean(out.text)

        for label, route in (("list", ("GET", "/api/v1/variables/")), ("create", ("POST", "/api/v1/variables/"))):
            with self.subTest(label):
                self.reset_state()
                self.lf.routes[("GET", "/api/v1/variables/")] = json_response([])
                self.lf.routes[route] = http_error(500)
                with self.capture() as out:
                    bootstrap.step_langflow_vars(BASE, NEW_KEY)
                self.assertTrue(bootstrap._failed)
                self.assertIn("HTTP 500", out.stdout)
                self.assert_clean(out.text)

    def test_a_generic_variable_that_differs_from_env_is_updated_and_an_equal_one_is_not(self):
        env = read_env(self.env_path)
        self.lf.routes[("GET", "/api/v1/variables/")] = json_response([
            {"id": "v1", "name": "POSTGRES_DB_USER", "type": "Generic", "value": "edu_ai"},
            {"id": "v2", "name": "POSTGRES_DB", "type": "Generic", "value": env["POSTGRES_DB"]},
            {"id": "v3", "name": "POSTGRES_DB_SERVER", "type": "Credential", "value": None}])
        self.lf.routes[("PATCH", "/api/v1/variables/v1")] = json_response({"id": "v1"})
        with self.capture() as out:
            bootstrap.step_langflow_vars(BASE, NEW_KEY)
        patches = [c for c in self.lf.calls if c.method == "PATCH"]
        self.assertEqual([c.path for c in patches], ["/api/v1/variables/v1"])
        self.assertEqual(json.loads(patches[0].body)["value"], env["POSTGRES_DB_USER"])
        self.assertFalse(bootstrap._failed)
        self.assertIn("POSTGRES_DB_USER", out.stdout)
        self.assert_clean(out.text)

    def test_a_malformed_variable_list_is_a_failure_not_a_crash(self):
        self.lf.routes[("GET", "/api/v1/variables/")] = json_response({"not": "a list of objects"})
        with self.capture() as out:
            bootstrap.step_langflow_vars(BASE, NEW_KEY)
        self.assertTrue(bootstrap._failed)
        self.assert_clean(out.text)

    def test_flows_import_receives_the_key_but_a_failure_shows_no_body(self):
        seen = {}

        def child(args, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(args, 0, f"Importing 6 flows\n  + a ({PG_PASSWORD})\n", "")
        with mock.patch.object(bootstrap, "run", side_effect=child), self.capture() as out:
            bootstrap.step_flows(BASE, NEW_KEY)
        self.assertEqual(seen["env"]["LANGFLOW_API_KEY"], NEW_KEY)
        self.assertEqual(seen["env"]["LANGFLOW_URL"], BASE)
        self.assertIn("Importing 6 flows", out.stdout)
        self.assert_clean(out.text)
        self.assertFalse(bootstrap._failed)

        body = f"ERROR POST /api/v1/flows/: HTTP 500 — {HOSTILE_BODY.decode()}"
        failing = subprocess.CompletedProcess([], 1, "", body)
        with mock.patch.object(bootstrap, "run", return_value=failing), self.capture() as out:
            bootstrap.step_flows(BASE, NEW_KEY)
        self.assertTrue(bootstrap._failed)
        self.assertIn("exit 1", out.stdout)
        self.assert_clean(out.text)

    def test_smoke_failures_keep_their_label_and_lose_their_details(self):
        report_text = "\n".join([
            "1. container health", "  [pass] edu_ai: running (healthy)",
            f"  [FAIL] probe flow x returned HTTP 500: {HOSTILE_BODY.decode()}",
            f"  [FAIL] unexpected psql output: {OLD_KEY}",
            f"  [skip] probe not reachable ({SU_PASSWORD})", "", "3 passed, 2 failed, 1 skipped"])
        failing = subprocess.CompletedProcess([], 1, report_text, f"Traceback {BODY_SECRET}")
        with mock.patch.object(bootstrap, "run", return_value=failing), self.capture() as out:
            bootstrap.step_smoke(BASE)
        self.assertTrue(bootstrap._failed)
        self.assertIn("[pass] edu_ai: running (healthy)", out.stdout)
        self.assertIn("[FAIL] probe flow x returned HTTP 500 (details withheld)", out.stdout)
        self.assertIn("3 passed, 2 failed, 1 skipped", out.stdout)
        self.assert_clean(out.text)

    def test_env_step_records_only_what_it_generated_and_prints_no_value(self):
        self.example_path.write_text(SYNTHETIC_EXAMPLE, encoding="utf-8")
        self.env_path.unlink()
        with self.capture() as out:
            bootstrap.step_env()
        env = read_env(self.env_path)
        self.assertEqual(bootstrap._created_secrets, CREATED_NAMES)
        for name in CREATED_NAMES:
            self.assertFalse(bootstrap.is_placeholder(env[name]))
            self.assertNotIn(env[name], out.text)
        self.assertEqual(env["OPENAI_API_KEY"], OPENAI_KEY)
        self.assert_clean(out.text)

        # A re-run in a fresh process: nothing to generate, nothing recorded, file untouched.
        self.reset_state()
        before = self.env_state()
        with self.capture() as out:
            bootstrap.step_env()
        self.assertEqual(bootstrap._created_secrets, [])
        self.assertEqual(self.env_state(), before)
        self.assertNotIn("generated", out.stdout)

    def test_env_step_does_not_claim_a_value_that_already_existed(self):
        self.example_path.write_text(SYNTHETIC_EXAMPLE, encoding="utf-8")
        self.write_env(PGADMIN_DEFAULT_PASSWORD="CHANGE_ME_pgadmin_password",
                       LANGFLOW_SECRET_KEY="CHANGE_ME_langflow_secret_key")
        with self.capture():
            bootstrap.step_env()
        self.assertEqual(bootstrap._created_secrets, ["PGADMIN_DEFAULT_PASSWORD", "LANGFLOW_SECRET_KEY"])
        self.assertEqual(read_env(self.env_path)["POSTGRES_PASSWORD"], PG_PASSWORD)


# ───────────────────────────────────────────────────────────────
#  closing overview: interactive vs not
# ───────────────────────────────────────────────────────────────

class InteractiveDetectionTests(Offline):
    def detect(self, *, stdin_tty, stdout_tty, no_input=False, **env):
        with self.stdin(stdin_tty), mock.patch.object(sys, "stdout", TTYBuffer() if stdout_tty else io.StringIO()), \
                mock.patch.dict(os.environ, env):
            return bootstrap.interactive_terminal(no_input)

    def test_a_person_at_a_terminal(self):
        self.assertTrue(self.detect(stdin_tty=True, stdout_tty=True))
        for value in ("", "0", "false", "False", "no"):
            with self.subTest(CI=value):
                self.assertTrue(self.detect(stdin_tty=True, stdout_tty=True, CI=value))

    def test_not_when_redirected_in_ci_or_under_no_input(self):
        self.assertFalse(self.detect(stdin_tty=True, stdout_tty=False))
        self.assertFalse(self.detect(stdin_tty=False, stdout_tty=True))
        self.assertFalse(self.detect(stdin_tty=False, stdout_tty=False))
        self.assertFalse(self.detect(stdin_tty=True, stdout_tty=True, no_input=True))
        for name in bootstrap._CI_VARS:
            with self.subTest(name):
                self.assertFalse(self.detect(stdin_tty=True, stdout_tty=True, **{name: "true"}))
        self.assertFalse(self.detect(stdin_tty=True, stdout_tty=True, CI="1"))

    def test_a_replaced_or_closed_stream_is_not_a_terminal(self):
        with mock.patch.object(sys, "stdin", None):
            self.assertFalse(bootstrap.interactive_terminal(False))


class SummaryTests(Offline):
    ALL_CREATED = CREATED_NAMES + ["LANGFLOW_API_KEY"]

    def summary(self, *, created, tty=True, stdin_tty=True, no_input=False, failed=False):
        """Run the overview in a given terminal situation. Whether secrets may be
        shown is decided inside step_summary from the real streams and environment."""
        bootstrap._created_secrets[:] = created
        bootstrap._failed = failed
        with self.stdin(stdin_tty), self.capture(tty=tty) as out:
            bootstrap.step_summary(BASE, no_input=no_input)
        return out

    def value_of(self, name):
        return read_env(self.env_path)[name]

    def test_interactive_first_run_shows_exactly_what_it_created_once_at_the_end(self):
        self.write_env(LANGFLOW_API_KEY=NEW_KEY)
        out = self.summary(created=self.ALL_CREATED)
        text = out.stdout
        marker = text.index("Shown once")
        self.assertGreater(marker, text.index("Next steps"))
        before, after = text[:marker], text[marker:]
        for name in self.ALL_CREATED:
            self.assertEqual(after.count(self.value_of(name)), 1, name)
            self.assertNotIn(self.value_of(name), before)
        self.assertIn(str(self.env_path), before)
        self.assertEqual(out.stderr, "")
        # External material never joins them.
        for external in (OPENAI_KEY, CF_TOKEN, BODY_SECRET, PASTED_KEY):
            self.assertNotIn(external, text)

    def test_rerun_prints_no_existing_value(self):
        out = self.summary(created=[])
        self.assert_clean(out.text)
        self.assertIn(str(self.env_path), out.stdout)
        self.assertIn("a re-run never prints them", out.stdout)
        self.assertNotIn("Shown once", out.stdout)

    def test_only_the_values_created_now_are_shown(self):
        out = self.summary(created=["PGADMIN_DEFAULT_PASSWORD", "LANGFLOW_API_KEY"])
        self.assertIn(PGADMIN_PASSWORD, out.stdout)
        self.assertIn(OLD_KEY, out.stdout)
        for other in (PG_PASSWORD, SU_PASSWORD, FERNET, OPENAI_KEY, CF_TOKEN):
            self.assertNotIn(other, out.stdout)

    def test_a_value_that_is_gone_from_env_is_not_invented(self):
        self.write_env(POSTGRES_PASSWORD="CHANGE_ME_postgres_password")
        out = self.summary(created=["POSTGRES_PASSWORD", "LANGFLOW_SECRET_KEY"])
        self.assertIn(FERNET, out.stdout)
        self.assertNotIn("CHANGE_ME_postgres_password", out.stdout.split("Shown once")[1])

    def test_a_pasted_key_is_never_shown(self):
        self.write_env(LANGFLOW_API_KEY=PASTED_KEY)
        out = self.summary(created=CREATED_NAMES)
        self.assertNotIn(PASTED_KEY, out.text)
        self.assertIn(FERNET, out.stdout)

    def test_outside_an_interactive_terminal_only_the_path_and_names_are_given(self):
        situations = {
            "redirected stdout": dict(tty=False),
            "stdin is not a terminal": dict(stdin_tty=False),
            "--no-input": dict(no_input=True),
            "run with errors": dict(failed=True),
        }
        for label, kwargs in situations.items():
            with self.subTest(label):
                self.write_env(LANGFLOW_API_KEY=NEW_KEY)
                out = self.summary(created=self.ALL_CREATED, **kwargs)
                self.assert_clean(out.text)
                self.assertIn(str(self.env_path), out.stdout)
                self.assertIn("Created now", out.stdout)
                self.assertIn("LANGFLOW_SECRET_KEY", out.stdout)  # the name, not the value
                self.assertIn("not printed", out.stdout)
                self.assertNotIn("Shown once", out.stdout)

    def test_ci_never_shows_secrets_even_on_a_terminal(self):
        for name in bootstrap._CI_VARS:
            with self.subTest(name), mock.patch.dict(os.environ, {name: "true"}):
                self.write_env(LANGFLOW_API_KEY=NEW_KEY)
                out = self.summary(created=self.ALL_CREATED)
                self.assert_clean(out.text)
                self.assertIn(str(self.env_path), out.stdout)
                self.assertNotIn("Shown once", out.stdout)

    def test_the_overview_names_the_next_steps(self):
        bootstrap._summary.append("OPENAI_API_KEY is not set. Put a real key in .env.")
        out = self.summary(created=[])
        self.assertIn("Next steps", out.stdout)
        self.assertIn("OPENAI_API_KEY is not set", out.stdout)
        self.assertIn("LANGFLOW_SECRET_KEY in it must never change", out.stdout)

    def test_the_decision_cannot_be_forced_from_outside(self):
        import inspect
        self.assertEqual(list(inspect.signature(bootstrap.step_summary).parameters),
                         ["base_url", "no_input"])


# ───────────────────────────────────────────────────────────────
#  whole runs through main()
# ───────────────────────────────────────────────────────────────

class MainRunTests(Offline):
    ARGV = ["bootstrap.py", "--langflow-url", BASE, "--skip-smoke"]

    def setUp(self):
        super().setUp()
        self.example_path.write_text(SYNTHETIC_EXAMPLE, encoding="utf-8")
        self.stages = {}
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for name in ("step_preflight", "step_dirs", "step_langflow_vars", "step_flows", "step_smoke"):
            self.stages[name] = stack.enter_context(mock.patch.object(bootstrap, name))
        self.stages["step_up"] = stack.enter_context(
            mock.patch.object(bootstrap, "step_up", return_value=True))

    def run_main(self, *extra, tty, stdin_tty=None):
        with self.stdin(tty if stdin_tty is None else stdin_tty), \
                mock.patch.object(sys, "argv", [*self.ARGV, *extra]), self.capture(tty=tty) as out:
            code = bootstrap.main()
        return code, out

    def generated(self):
        env = read_env(self.env_path)
        return {name: env[name] for name in self.created_names()}

    @staticmethod
    def created_names():
        return CREATED_NAMES + ["LANGFLOW_API_KEY"]

    def first_install(self, *, tty, extra=()):
        self.env_path.unlink(missing_ok=True)
        return self.run_main(*extra, tty=tty)

    def test_first_install_in_a_terminal_shows_the_new_secrets_once_and_keeps_them_in_env(self):
        code, out = self.first_install(tty=True)
        self.assertEqual(code, 0)
        values = self.generated()
        self.assertEqual(values["LANGFLOW_API_KEY"], NEW_KEY)
        logs, overview = out.stdout.split("edu_ai is up.")
        for value in values.values():
            self.assertNotIn(value, logs)       # progress lines
            self.assertNotIn(value, out.stderr)  # error stream
            self.assertEqual(overview.count(value), 1)
        for external in (OPENAI_KEY, CF_TOKEN, BODY_SECRET):
            self.assertNotIn(external, out.text)
        self.assertIn(str(self.env_path), overview)
        # The file is still the permanent source and holds every one of them.
        for name, value in values.items():
            self.assertEqual(read_env(self.env_path)[name], value)
        # The key reaches the later steps through memory, not through the output.
        self.stages["step_langflow_vars"].assert_called_once_with(BASE, NEW_KEY)
        self.stages["step_flows"].assert_called_once_with(BASE, NEW_KEY)

    def test_first_install_in_ci_or_redirected_output_shows_only_the_path(self):
        modes = {
            "redirected stdout": dict(tty=False, stdin_tty=True),
            "no stdin terminal": dict(tty=True, stdin_tty=False),
            "--no-input": dict(tty=True, extra=("--no-input",)),
            "CI": dict(tty=True, ci="true"),
            "GITHUB_ACTIONS": dict(tty=True, ci="true", ci_var="GITHUB_ACTIONS"),
        }
        for label, mode in modes.items():
            with self.subTest(label):
                self.reset_state()
                self.lf.calls.clear()
                self.env_path.unlink(missing_ok=True)
                env = {mode.get("ci_var", "CI"): mode["ci"]} if "ci" in mode else {}
                with mock.patch.dict(os.environ, env):
                    code, out = self.run_main(*mode.get("extra", ()), tty=mode["tty"],
                                              stdin_tty=mode.get("stdin_tty"))
                self.assertEqual(code, 0)
                for name, value in self.generated().items():
                    self.assertNotIn(value, out.text, f"{name} leaked in {label}")
                self.assertNotIn("Shown once", out.stdout)
                self.assertIn(str(self.env_path), out.stdout)
                self.assertIn("Created now", out.stdout)
                self.assert_clean(out.text)

    def test_a_rerun_prints_nothing_that_already_existed(self):
        self.first_install(tty=True)
        values = self.generated()
        self.reset_state()
        self.lf.calls.clear()
        before = self.env_state()
        code, out = self.run_main(tty=True)
        self.assertEqual(code, 0)
        for value in values.values():
            self.assertNotIn(value, out.text)
        self.assertNotIn("Shown once", out.stdout)
        self.assertEqual(self.env_state(), before)
        self.assertEqual(self.lf.mutating(), [])
        self.assert_clean(out.text)

    def test_a_run_that_ends_with_errors_shows_no_secret(self):
        self.stages["step_flows"].side_effect = lambda *a: bootstrap.fail("import_flows.py failed (exit 1)")
        self.env_path.unlink(missing_ok=True)
        code, out = self.run_main(tty=True)
        self.assertEqual(code, 1)
        for value in self.generated().values():
            self.assertNotIn(value, out.text)
        self.assertIn("WITH ERRORS", out.stdout)
        self.assertNotIn("Shown once", out.stdout)

    def test_a_pasted_key_is_not_shown_in_the_first_run_overview(self):
        self.env_path.unlink(missing_ok=True)
        self.lf.routes[("POST", "/api/v1/login")] = http_error(401)
        with mock.patch.object(cak.getpass, "getpass", return_value=PASTED_KEY):
            code, out = self.run_main(tty=True)
        self.assertEqual(code, 0)
        self.assertNotIn(PASTED_KEY, out.text)
        overview = out.stdout.split("Shown once")[1]
        for name in CREATED_NAMES:
            self.assertIn(read_env(self.env_path)[name], overview)
        self.assertNotIn("LANGFLOW_API_KEY", overview)

    def test_unverifiable_key_stops_the_run_with_status_3_before_anything_else(self):
        for label, behavior, _ in unverifiable_cases():
            with self.subTest(label):
                self.reset_state()
                self.lf.calls.clear()
                for stage in self.stages.values():
                    stage.reset_mock()
                self.write_env()
                self.lf.routes[("GET", "/api/v1/flows/")] = behavior
                before = self.env_state()
                with self.no_prompt(tty=True) as prompt:
                    code, out = self.run_main(tty=True)
                self.assertEqual(code, cak.EXIT_RETRY_LATER)
                self.assertEqual(self.env_state(), before)
                self.assertEqual(self.lf.mutating(), [])
                prompt.assert_not_called()
                self.assertEqual(self.stages["step_up"].call_count, 1)  # the core services only
                for later in ("step_langflow_vars", "step_flows", "step_smoke"):
                    self.stages[later].assert_not_called()
                self.assertNotIn("edu_ai is up.", out.stdout)
                self.assertNotIn("Shown once", out.stdout)
                self.assert_clean(out.text)

    def test_a_plain_failure_is_still_status_1(self):
        self.write_env(LANGFLOW_API_KEY=None)
        self.lf.routes[("POST", "/api/v1/login")] = http_error(500)
        code, out = self.run_main("--no-input", tty=False)
        self.assertEqual(code, 1)
        self.assert_clean(out.text)


if __name__ == "__main__":
    unittest.main()
