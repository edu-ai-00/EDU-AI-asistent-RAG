"""B06 bootstrap: key rotation order (N4), no redirect for secret-bearing calls (N5), explicit env_path (N8)
and the retry exit code through the callers (N9). The publication phases, the 403 rule and the rule that
no old key is ever deleted are in test_bootstrap_key_lifecycle.py.

Run: python tests/test_bootstrap_key_rotation.py   (add -v for names)

Offline: HTTP is the fake of test_bootstrap_key_safety.py (a mock of urllib, NOT Langflow 1.10.1) or, for the
redirect tests, a fake transport placed under urllib's real redirect machinery. No sockets, no subprocess, no
Docker, never the repository's real .env. Synthetic secrets only.

What it pins down
  * a new key is created, accepted by the same request that rejected the old one, and only then stored; a failure
    before the store leaves .env (bytes, mtime) and every old key alone, with --force too, and the new key, which
    was never stored, is deleted again (by the id of the reply, or the one entry that appeared; never a guess);
    no old key is ever deleted: a masked listing cannot identify it;
  * a pasted key is checked before it is stored; a key that cannot be sent as a header is not sent;
  * a 3xx is never followed by a call that carries a key or token, for every redirect status; a standard opener,
    used as a control, does forward the header, so the test can see the leak it prevents;
  * ensure_api_key has no default env_path and every caller passes one;
  * an unverifiable key makes bootstrap.main() return 3 before anything else starts; an ordinary failure returns 1.
"""
import ast
import errno
import http.client
import inspect
import io
import pathlib
import sys
import unittest
import urllib.error
import urllib.request
import urllib.response
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import test_bootstrap_key_safety as ks  # noqa: E402  (module, not its classes: nothing is collected twice)
import bootstrap  # noqa: E402
import create_api_key as cak  # noqa: E402
import http_safe  # noqa: E402
from envfile import read_env  # noqa: E402

SCRIPTS = HERE.parent / "scripts"
BASE, OLD_KEY, NEW_KEY = ks.BASE, ks.OLD_KEY, ks.NEW_KEY
OLD_MASK, NEW_MASK, OTHER_MASK = OLD_KEY[:8] + "****", NEW_KEY[:8] + "****", "sk-oth-e****"
KEYS_PATH = "/api/v1/api_key/"
FLOWS_CHECK = ("GET", "/api/v1/flows/?page=1&size=1")


class Rotation(ks.Offline):
    def run_ensure(self, **kwargs):
        kwargs.setdefault("allow_prompt", False)
        return cak.ensure_api_key(BASE, self.env_path, **kwargs)

    def key_in_env(self):
        return read_env(self.env_path).get("LANGFLOW_API_KEY")

    def deletes(self):
        return [c.path for c in self.lf.calls if c.method == "DELETE"]

    def reject_old_key(self, code=401):
        self.lf.flows_by_key[OLD_KEY] = ks.http_error(code)

    def listing(self, entries, *, extra_after_creation=((("k9", NEW_MASK)),)):
        """The key listing: `entries` are (id, name, masked value). After a creation it also shows what Langflow
        would show for the new key: `extra_after_creation` is a sequence of (id, masked value)."""
        def answer():
            created = any(c.method == "POST" and c.path == KEYS_PATH for c in self.lf.calls)
            keys = [{"id": i, "name": n, "api_key": m} for i, n, m in entries]
            if created:
                keys += [{"id": i, "name": cak.KEY_NAME, "api_key": m} for i, m in extra_after_creation]
            return ks.json_response({"api_keys": keys})
        self.lf.routes[("GET", KEYS_PATH)] = answer

    def standard_listing(self):
        self.listing([("k1", cak.KEY_NAME, OLD_MASK), ("k2", "someone-elses", "sk-els-e****")])


class OrderTests(Rotation):
    def test_a_rejected_key_is_replaced_in_the_safe_order(self):
        seen = {}

        def record(call):
            if call.method == "GET" and call.headers.get("x-api-key") == NEW_KEY:
                seen["env_at_check"] = self.key_in_env()
        self.lf.on_call = record
        self.reject_old_key()
        with self.capture() as out:
            result = self.run_ensure()
        self.assertEqual(result, (NEW_KEY, "replaced"))
        self.assertEqual(self.lf.paths(), [
            FLOWS_CHECK, ("POST", "/api/v1/login"), ("GET", KEYS_PATH), ("POST", KEYS_PATH), FLOWS_CHECK])
        self.assertEqual(self.lf.calls[0].headers["x-api-key"], OLD_KEY)          # the old key is what was rejected
        self.assertEqual(self.lf.calls[4].headers["x-api-key"], NEW_KEY)          # the new key is what was accepted
        self.assertEqual(seen["env_at_check"], OLD_KEY, ".env was written before the new key was accepted")
        self.assertEqual(self.key_in_env(), NEW_KEY)
        self.assertEqual(self.deletes(), [])
        self.assert_clean(out.text)

    def test_with_no_old_key_in_env_nothing_is_deleted_even_if_own_named_keys_exist(self):
        self.write_env(LANGFLOW_API_KEY=None)
        self.listing([("k1", cak.KEY_NAME, OLD_MASK), ("k3", cak.KEY_NAME, OTHER_MASK)])
        with self.capture() as out:
            self.assertEqual(self.run_ensure(), (NEW_KEY, "created"))
        self.assertEqual(self.deletes(), [])
        self.assertEqual(self.lf.paths(), [("POST", "/api/v1/login"), ("GET", KEYS_PATH), ("POST", KEYS_PATH), FLOWS_CHECK])
        self.assertNotIn("previous key", out.stderr)          # there was none to leave behind

    def test_a_failed_listing_does_not_block_the_replacement(self):
        self.reject_old_key()
        self.lf.routes[("GET", KEYS_PATH)] = ks.http_error(500)
        with self.capture() as out:
            self.assertEqual(self.run_ensure(), (NEW_KEY, "replaced"))
        self.assertEqual(self.deletes(), [])
        self.assertEqual(self.key_in_env(), NEW_KEY)
        self.assertIn("previous key was left in Langflow", out.stderr)


FAILURES = {
    "login is refused": [("POST", "/api/v1/login", ks.http_error(401))],
    "login times out": [("POST", "/api/v1/login", urllib.error.URLError(TimeoutError("t")))],
    "the key creation fails (500)": [("POST", KEYS_PATH, ks.http_error(500))],
    "the key creation answers without a key": [("POST", KEYS_PATH, ks.json_response({"x": 1}))],
}
VERIFY_FAILURES = {
    "the new key is rejected (401)": ks.http_error(401),
    "the new key is rejected (403, a proxy would say the same)": ks.http_error(403),
    "the check fails (500)": ks.http_error(500),
    "the check times out": urllib.error.URLError(TimeoutError("t")),
    "the check answers an html page": ks.FakeResponse(200, b"<html>sign in</html>"),
}


class FailureTests(Rotation):
    """A failure before the store leaves .env and every old key alone, with and without --force."""

    def modes(self):
        yield "rejected key", {}, self.reject_old_key
        yield "--force on a working key", {"force": True}, lambda: None

    def attempt(self, kwargs, *, error=RuntimeError, discarded=()):
        before, listing = self.env_state(), sorted(p.name for p in self.tmp.iterdir())
        with self.stdin(False), self.capture() as out, self.assertRaises(error) as caught:
            self.run_ensure(allow_prompt=True, **kwargs)
        self.assertEqual(self.env_state(), before)                                # bytes and mtime
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), listing)     # no temporary file
        self.assertEqual(self.deletes(), [KEYS_PATH + i for i in discarded],
                         "only the key that was just created and not stored may be deleted")
        self.assertNotIn(KEYS_PATH + "k1", self.deletes())                        # the old key is never touched
        self.assert_clean(str(caught.exception) + out.text)
        return caught.exception, out

    def test_a_failure_in_login_or_creation(self):
        for mode, kwargs, prepare in self.modes():
            for label, routes in FAILURES.items():
                with self.subTest(mode=mode, failure=label):
                    self.lf.calls.clear()
                    self.standard_listing()
                    self.write_env()
                    prepare()
                    for method, path, behaviour in routes:
                        self.lf.routes[(method, path)] = behaviour
                    exc, _ = self.attempt(kwargs)
                    self.assertNotIsInstance(exc, cak.KeyUnverifiable)

    def test_a_new_key_that_cannot_be_confirmed_is_not_stored_is_deleted_again_and_ends_in_3(self):
        for mode, kwargs, prepare in self.modes():
            for label, behaviour in VERIFY_FAILURES.items():
                with self.subTest(mode=mode, failure=label):
                    self.lf.calls.clear()
                    self.write_env()
                    prepare()
                    self.standard_listing()
                    self.lf.flows_by_key[NEW_KEY] = behaviour
                    exc, out = self.attempt(kwargs, error=cak.KeyUnverifiable, discarded=["k9"])
                    self.assertEqual(sum(1 for c in self.lf.calls if c.method == "POST" and c.path == KEYS_PATH), 1)
                    self.assertIn("was removed again", str(exc))
                    self.assertIn("Nothing was changed", str(exc))

    def test_the_new_key_is_deleted_by_the_id_of_the_creation_reply_when_there_is_one(self):
        self.write_env()
        self.reject_old_key()
        self.standard_listing()
        self.lf.routes[("POST", KEYS_PATH)] = ks.json_response({"api_key": NEW_KEY, "id": "k7"})
        self.lf.flows_by_key[NEW_KEY] = ks.http_error(500)
        self.attempt({}, error=cak.KeyUnverifiable, discarded=["k7"])

    def test_the_new_key_is_never_guessed_between_several_new_entries(self):
        self.write_env()
        self.reject_old_key()
        self.listing([("k1", cak.KEY_NAME, OLD_MASK)], extra_after_creation=(("k8", NEW_MASK), ("k9", NEW_MASK)))
        self.lf.flows_by_key[NEW_KEY] = ks.http_error(500)
        self.attempt({}, error=cak.KeyUnverifiable, discarded=[])

    def test_a_failed_store_keeps_the_previous_env_never_prompts_and_deletes_only_the_new_key(self):
        for mode, kwargs, prepare in self.modes():
            with self.subTest(mode=mode):
                self.lf.calls.clear()
                self.write_env()
                prepare()
                self.standard_listing()
                before, listing = self.env_state(), sorted(p.name for p in self.tmp.iterdir())
                with self.no_prompt(tty=True) as prompt, self.capture() as out, \
                        mock.patch("os.replace", side_effect=OSError(errno.ENOSPC, f"full {NEW_KEY}")), \
                        self.assertRaises(cak.KeyStoreError) as caught:
                    self.run_ensure(allow_prompt=True, **kwargs)
                prompt.assert_not_called()
                self.assertEqual(self.env_state(), before)
                self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), listing)
                self.assertEqual(self.deletes(), [KEYS_PATH + "k9"])
                self.assertIn(".env is unchanged (compared byte for byte)", str(caught.exception))
                self.assertNotIsInstance(caught.exception, cak.KeyStateUncertain)
                self.assert_clean(str(caught.exception) + out.text)

    def test_an_unconfirmed_write_is_an_uncertain_state_and_keeps_the_new_key(self):
        self.write_env(LANGFLOW_API_KEY=None)
        self.standard_listing()
        real, calls = cak.read_env, []

        def read_back_wrong(path):
            calls.append(path)
            return real(path) if len(calls) == 1 else {}        # the first read is ensure_api_key's own

        with mock.patch.object(cak, "read_env", read_back_wrong), self.no_prompt(tty=True), self.capture(), \
                self.assertRaises(cak.KeyStateUncertain) as caught:
            self.run_ensure(allow_prompt=True)
        self.assertIsInstance(caught.exception, cak.KeyStoreError)
        self.assertEqual(self.deletes(), [])                    # .env does hold the new key: it must stay valid
        self.assertEqual(self.key_in_env(), NEW_KEY)

    def test_an_interrupt_after_the_creation_deletes_the_new_key_and_propagates(self):
        self.write_env()
        self.standard_listing()
        before = self.env_state()
        with mock.patch.object(cak, "check_key", side_effect=KeyboardInterrupt), self.capture(), \
                self.assertRaises(KeyboardInterrupt):
            self.run_ensure(force=True)
        self.assertEqual(self.env_state(), before)
        self.assertEqual(self.deletes(), [KEYS_PATH + "k9"])

    def test_a_pasted_key_is_checked_before_it_is_stored(self):
        self.write_env()
        self.reject_old_key()
        self.lf.routes[("POST", "/api/v1/login")] = ks.http_error(401)
        self.lf.flows_by_key[ks.PASTED_KEY] = ks.http_error(403)
        before = self.env_state()
        with self.stdin(True), mock.patch.object(cak.getpass, "getpass", return_value=ks.PASTED_KEY), \
                self.capture() as out, self.assertRaises(cak.KeyUnverifiable) as caught:
            self.run_ensure(allow_prompt=True)
        self.assertEqual(self.env_state(), before)
        self.assertIn("Nothing was changed", str(caught.exception))
        self.assertEqual(self.deletes(), [])
        self.assert_clean(str(caught.exception) + out.text, allowed=(ks.PASTED_KEY,))
        self.assertNotIn(ks.PASTED_KEY, str(caught.exception) + out.text)

    def test_a_pasted_key_that_is_accepted_never_deletes_anything(self):
        self.write_env()
        self.reject_old_key()
        self.lf.routes[("POST", "/api/v1/login")] = ks.http_error(401)
        with self.stdin(True), mock.patch.object(cak.getpass, "getpass", return_value=ks.PASTED_KEY), self.capture():
            self.assertEqual(self.run_ensure(allow_prompt=True), (ks.PASTED_KEY, "manual"))
        self.assertEqual(self.deletes(), [])
        self.assertEqual([m for m, _ in self.lf.paths() if m != "GET"], ["POST"])      # only the refused login
        self.assertEqual(self.key_in_env(), ks.PASTED_KEY)

    def test_a_key_that_cannot_be_sent_is_not_sent_and_changes_nothing(self):
        for bad in ("sk-ž-not-ascii", "sk-ctrl\x00char", "sk-tab\tchar"):
            with self.subTest(key=repr(bad)):
                self.lf.calls.clear()
                self.write_env(LANGFLOW_API_KEY=bad)
                before = self.env_state()
                with self.no_prompt(tty=True), self.capture() as out, self.assertRaises(cak.KeyUnverifiable) as caught:
                    self.run_ensure(allow_prompt=True)
                self.assertEqual(self.lf.calls, [], "Langflow must not be asked with a key a header cannot carry")
                self.assertEqual(self.env_state(), before)
                self.assertIn("cannot be sent", str(caught.exception))
                self.assertNotIn(bad, str(caught.exception) + out.text)


class ForceTests(Rotation):
    def test_force_never_asks_about_the_old_key_and_deletes_no_old_key(self):
        self.listing([("k1", cak.KEY_NAME, OLD_MASK), ("k2", "ui-created-key", OLD_MASK), ("k3", cak.KEY_NAME, OTHER_MASK)])
        with self.capture() as out:
            self.assertEqual(self.run_ensure(force=True), (NEW_KEY, "created"))
        self.assertTrue(all(c.headers.get("x-api-key") != OLD_KEY for c in self.lf.calls), "the old key was used")
        self.assertEqual(self.deletes(), [])
        self.assertEqual(self.key_in_env(), NEW_KEY)
        self.assertIn("previous key was left in Langflow", out.stderr)
        self.assert_clean(out.text)


class RedirectingHTTP(urllib.request.HTTPHandler):
    """The transport under urllib's real redirect machinery: BASE answers a 3xx to another host, which answers 200."""

    def __init__(self, code=302):
        super().__init__()
        self.code = code
        self.requests = []

    def http_open(self, req):
        self.requests.append((req.full_url, {k.lower(): v for k, v in req.header_items()}, req.get_method()))
        headers = http.client.HTTPMessage()
        if req.full_url.startswith(BASE):
            headers["Location"] = "http://evil.test/steal"
            code, msg = self.code, "Redirect"
        else:
            code, msg = 200, "OK"
        response = urllib.response.addinfourl(io.BytesIO(b"{}"), headers, req.full_url, code)
        response.msg = msg
        return response

    def leaked(self):
        """Requests that went anywhere but BASE, and which secret headers they carried."""
        return [(url, sorted(set(h) & {"x-api-key", "authorization"})) for url, h, _ in self.requests if not url.startswith(BASE)]


REDIRECTS = (301, 302, 303, 307, 308)


class RedirectTests(Rotation):
    def refusing(self, code=302):
        transport = RedirectingHTTP(code)
        stack = mock.patch.object(http_safe, "OPENER", http_safe.make_opener(transport))
        stack.start()
        self.addCleanup(stack.stop)
        return transport

    def test_the_control_a_standard_opener_forwards_the_key_to_the_other_host(self):
        for code in (302, 307):
            with self.subTest(code=code):
                transport = RedirectingHTTP(code)
                opener = urllib.request.build_opener(transport)
                request = urllib.request.Request(f"{BASE}/api/v1/flows/", headers={"x-api-key": OLD_KEY})
                opener.open(request, timeout=5)
                self.assertEqual(transport.leaked(), [("http://evil.test/steal", ["x-api-key"])],
                                 "the control no longer shows the leak: this test cannot prove anything")

    def test_every_redirect_status_surfaces_as_an_error_and_sends_nothing_elsewhere(self):
        for code in REDIRECTS:
            with self.subTest(code=code):
                transport = RedirectingHTTP(code)
                request = urllib.request.Request(f"{BASE}/x", headers={"x-api-key": OLD_KEY, "Authorization": "Bearer t"})
                with mock.patch.object(http_safe, "OPENER", http_safe.make_opener(transport)):
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        http_safe.open_url(request, timeout=5)
                self.assertEqual(caught.exception.code, code)
                caught.exception.close()
                self.assertEqual(len(transport.requests), 1)
                self.assertEqual(transport.leaked(), [])

    def test_a_redirect_is_not_followed_for_a_post_with_a_body_either(self):
        transport = RedirectingHTTP(307)
        request = urllib.request.Request(f"{BASE}/api/v1/login", data=b"password=x", method="POST")
        with mock.patch.object(http_safe, "OPENER", http_safe.make_opener(transport)):
            with self.assertRaises(urllib.error.HTTPError):
                http_safe.open_url(request, timeout=5)
        self.assertEqual(transport.leaked(), [])

    def test_the_key_check_calls_a_redirect_unverifiable_with_a_hint_and_sends_nothing_elsewhere(self):
        for code in REDIRECTS:
            with self.subTest(code=code):
                transport = self.refusing(code)
                check = cak.check_key(BASE, OLD_KEY)
                self.assertEqual(check.status, cak.KEY_UNVERIFIABLE)
                self.assertTrue(check.detail.startswith(f"HTTP {code} redirect, not followed"), check.detail)
                self.assertIn("LANGFLOW_URL", check.detail)
                self.assertEqual(transport.leaked(), [])

    def test_every_authenticated_call_of_create_api_key_refuses_a_redirect(self):
        transport = self.refusing()
        for label, call in {
            "login": lambda: cak.login(BASE, "admin", ks.SU_PASSWORD),
            "request": lambda: cak._request(f"{BASE}{KEYS_PATH}", headers={"Authorization": "Bearer t"}),
            "create": lambda: cak.create_key(BASE, "t"),
        }.items():
            with self.subTest(label), self.assertRaises(urllib.error.HTTPError) as caught:
                call()
            caught.exception.close()
        self.assertIsNone(cak.own_keys(BASE, "t"))              # a refused listing is "unknown", not "empty"
        self.assertEqual(cak.delete_keys(BASE, "t", ["k1"]), 1)
        self.assertEqual(transport.leaked(), [])

    def test_ensure_api_key_through_a_redirect_changes_nothing(self):
        transport = self.refusing()
        before = self.env_state()
        with self.no_prompt(tty=True), self.capture() as out, self.assertRaises(cak.KeyUnverifiable):
            self.run_ensure(allow_prompt=True)
        self.assertEqual(self.env_state(), before)
        self.assertEqual(transport.leaked(), [])
        self.assert_clean(out.text)

    def test_bootstrap_langflow_variables_refuse_a_redirect(self):
        transport = self.refusing()
        with self.capture() as out:
            bootstrap.step_langflow_vars(BASE, OLD_KEY)
        self.assertTrue(bootstrap._failed)
        self.assertEqual(transport.leaked(), [])
        self.assert_clean(out.text)

    def test_no_function_that_builds_an_authenticated_request_calls_urlopen_directly(self):
        """import_flows.py and smoke.py cannot be imported offline (they read the real .env at import), so their
        authenticated helpers are checked in the source: the header, and a direct urlopen, never in one function."""
        needed = {"create_api_key.py": {"_request", "check_key"}, "bootstrap.py": {"step_langflow_vars"},
                  "import_flows.py": {"call"}, "smoke.py": {"http_json"}}
        found = {name: set() for name in needed}
        for name in needed:
            tree = ast.parse((SCRIPTS / name).read_text(encoding="utf-8"))
            for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
                text = {n.value for n in ast.walk(func) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
                attrs = {n.attr for n in ast.walk(func) if isinstance(n, ast.Attribute)}
                names = {n.id for n in ast.walk(func) if isinstance(n, ast.Name)}
                carries_secret = bool(text & {"x-api-key", "Authorization"}) or func.name in needed[name]
                if carries_secret:
                    self.assertNotIn("urlopen", attrs, f"{name}:{func.name} calls urlopen directly with a secret")
                if func.name in needed[name]:
                    self.assertIn("open_url", names, f"{name}:{func.name} does not use open_url")
                    found[name].add(func.name)
        self.assertEqual(found, needed)


class EnvPathTests(Rotation):
    def test_ensure_api_key_has_no_default_env_path(self):
        parameter = inspect.signature(cak.ensure_api_key).parameters["env_path"]
        self.assertIs(parameter.default, inspect.Parameter.empty)
        with self.assertRaises(TypeError):
            cak.ensure_api_key(BASE)

    def test_every_caller_in_the_scripts_passes_it(self):
        callers = 0
        for path in sorted(SCRIPTS.glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "ensure_api_key":
                    callers += 1
                    self.assertTrue(len(node.args) >= 2 or any(k.arg == "env_path" for k in node.keywords),
                                    f"{path.name}:{node.lineno} does not pass env_path")
        self.assertGreaterEqual(callers, 2)                  # bootstrap.step_api_key and create_api_key.main

    def test_bootstrap_and_the_command_line_pass_their_own_path(self):
        with mock.patch.object(bootstrap, "ensure_api_key", return_value=(OLD_KEY, "skip")) as ensure, self.capture():
            bootstrap.step_api_key(BASE, allow_prompt=False)
        self.assertEqual(ensure.call_args.args, (BASE, self.env_path))
        with mock.patch.object(cak, "ensure_api_key", return_value=(OLD_KEY, "skip")) as ensure, \
                mock.patch.object(sys, "argv", ["create_api_key.py", "--url", BASE]), self.capture():
            cak.main()
        self.assertEqual(ensure.call_args.args, (BASE, self.env_path))


class ExitCodeTests(Rotation):
    """What bootstrap and the command do today with an unverifiable key; a wrapper will have to treat 3 as 'retry'."""

    def run_main(self):
        with mock.patch.object(sys, "argv", ["bootstrap.py", "--no-input", "--langflow-url", BASE]), \
                mock.patch.object(bootstrap, "step_preflight"), mock.patch.object(bootstrap, "step_env"), \
                mock.patch.object(bootstrap, "step_dirs"), \
                mock.patch.object(bootstrap, "step_up", return_value=True) as up, \
                mock.patch.object(bootstrap, "step_langflow_vars") as variables, \
                mock.patch.object(bootstrap, "step_flows") as flows, mock.patch.object(bootstrap, "step_smoke") as smoke, \
                self.capture() as out:
            code = bootstrap.main()
        return code, up, (variables, flows, smoke), out

    def test_an_unverifiable_key_returns_3_and_starts_nothing_more(self):
        for label, behaviour in (("503", ks.http_error(503)), ("timeout", urllib.error.URLError(TimeoutError("t"))),
                                 ("redirect", ks.http_error(302)), ("403", ks.http_error(403))):
            with self.subTest(label):
                self.lf.calls.clear()
                self.lf.routes[("GET", "/api/v1/flows/")] = behaviour
                before = self.env_state()
                code, up, later, out = self.run_main()
                self.assertEqual(code, 3)
                self.assertEqual(up.call_count, 1, "only the first start may run")
                for step in later:
                    step.assert_not_called()
                self.assertEqual(self.env_state(), before)
                self.assertEqual(self.lf.mutating(), [])
                self.assertNotIn("edu_ai is up", out.stdout)
                self.assert_clean(out.text)
                self.reset_state()

    def test_a_new_key_that_is_not_accepted_also_returns_3(self):
        self.reject_old_key(401)
        self.standard_listing()
        self.lf.flows_by_key[NEW_KEY] = ks.http_error(403)         # a proxy refusing everything
        before = self.env_state()
        code, up, later, out = self.run_main()
        self.assertEqual(code, 3)
        self.assertEqual(self.env_state(), before)
        self.assertEqual(self.deletes(), [KEYS_PATH + "k9"])
        for step in later:
            step.assert_not_called()

    def test_an_ordinary_failure_returns_1_not_3(self):
        self.write_env(LANGFLOW_API_KEY=None)
        self.lf.routes[("POST", "/api/v1/login")] = ks.http_error(401)
        code, up, later, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(up.call_count, 1)

    def test_the_commands_exit_through_main(self):
        tree = ast.parse((SCRIPTS / "bootstrap.py").read_text(encoding="utf-8"))
        last = tree.body[-1]
        self.assertEqual(ast.unparse(last.test), "__name__ == '__main__'")
        self.assertEqual(ast.unparse(last.body[0]), "sys.exit(main())")
        self.assertEqual(bootstrap.EXIT_RETRY_LATER, cak.EXIT_RETRY_LATER)
        self.assertEqual(cak.EXIT_RETRY_LATER, 3)

    def test_help_documents_the_exit_status_of_both_commands(self):
        for module, argv in ((bootstrap, ["bootstrap.py", "--help"]), (cak, ["create_api_key.py", "--help"])):
            with self.subTest(module=module.__name__), mock.patch.object(sys, "argv", argv), \
                    self.capture() as out, self.assertRaises(SystemExit) as caught:
                (module.main)()
            self.assertEqual(caught.exception.code, 0)
            self.assertIn("Exit status", out.stdout)
            self.assertIn("3", out.stdout)


if __name__ == "__main__":
    unittest.main()
