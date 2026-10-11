"""Installation input CHAT_LEGAL_ENTITY: the administrator supplies a real entity; install, bootstrap and smoke expose a missing or invalid one.

Offline: synthetic values, mocked Docker/psql, no real .env. Run: python3 tests/test_chat_entity_input.py
"""
from __future__ import annotations

import contextlib
import io
import pathlib
import re
import subprocess
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import bootstrap  # noqa: E402
import install  # noqa: E402
import smoke  # noqa: E402

TEMPLATE = (ROOT / ".env.example").read_text(encoding="utf-8")


def proc(out="", rc=0, err=""):
    return subprocess.CompletedProcess([], rc, out, err)


class Template(unittest.TestCase):
    def test_the_template_carries_a_placeholder_not_a_school(self):
        line = next(l for l in TEMPLATE.splitlines() if l.startswith("CHAT_LEGAL_ENTITY="))
        self.assertEqual(line, "CHAT_LEGAL_ENTITY=CHANGE_ME_legal_entity_code")
        self.assertFalse(re.search(r"^CHAT_LEGAL_ENTITY=(?!CHANGE_ME)", TEMPLATE, re.M))   # never a hard-coded entity

    def test_the_installer_reports_it_as_a_missing_manual_input(self):
        entries, _dups, _bad = install.parse_assignments(TEMPLATE)
        self.assertIn("CHAT_LEGAL_ENTITY", [k for k, v in entries.items() if install.is_placeholder(v)])

    def test_compose_still_passes_the_value_through_unchanged(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("CHAT_LEGAL_ENTITY=${CHAT_LEGAL_ENTITY:-}", compose)


class Bootstrap(unittest.TestCase):
    def test_step_env_warns_and_summarises_a_placeholder(self):
        source = (ROOT / "scripts" / "bootstrap.py").read_text(encoding="utf-8")
        body = source[source.index("def step_env"):source.index("def step_dirs")]
        self.assertIn('is_placeholder(final.get("CHAT_LEGAL_ENTITY"))', body)
        self.assertIn("CHAT_LEGAL_ENTITY is not set", body)
        self.assertIn("smoke test fails until", body)


class BootstrapStepEnv(unittest.TestCase):
    """step_env on a synthetic .env: browser selection sends nobody to SQL, .env or a CLI."""

    def run_step(self, extra: str) -> tuple[str, list[str]]:
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            (tmp / ".env.example").write_text(TEMPLATE, encoding="utf-8", newline="\n")
            (tmp / ".env").write_text(TEMPLATE + extra, encoding="utf-8", newline="\n")
            out = io.StringIO()
            with mock.patch.object(bootstrap, "ENV_PATH", tmp / ".env"), \
                    mock.patch.object(bootstrap, "ENV_EXAMPLE", tmp / ".env.example"), \
                    mock.patch.object(bootstrap, "_summary", []) as summary, \
                    mock.patch.object(bootstrap, "_created_secrets", []), \
                    mock.patch.object(bootstrap, "_failed", False), \
                    contextlib.redirect_stdout(out):
                bootstrap.step_env()
            return out.getvalue(), list(summary)

    def test_picker_without_a_fixed_school_is_not_a_warning_and_has_no_sql_or_env_instruction(self):
        text, summary = self.run_step("\nCHAT_ENTITY_PICKER=true\n")
        joined = " ".join(summary) + text
        self.assertNotIn("CHAT_LEGAL_ENTITY is still CHANGE_ME", text)
        for forbidden in ("SELECT", "legal_entity_code", "docker compose up -d edu_ai_web_chat",
                          "Put the code", "configure_chat"):
            self.assertNotIn(forbidden, joined)
        self.assertTrue(any("choose the school" in line for line in summary), summary)

    def test_without_the_picker_a_placeholder_school_is_still_reported_as_missing(self):
        text, summary = self.run_step("\nCHAT_ENTITY_PICKER=false\n")
        self.assertIn("CHAT_LEGAL_ENTITY is still CHANGE_ME", text)
        self.assertTrue(any("CHAT_LEGAL_ENTITY is not set" in line for line in summary), summary)

    def test_an_explicit_school_with_the_picker_off_is_left_alone(self):
        text, summary = self.run_step("\nCHAT_LEGAL_ENTITY=SYNTH_1\n")
        self.assertNotIn("CHAT_LEGAL_ENTITY", text)
        self.assertFalse(any("CHAT_LEGAL_ENTITY" in line or "choose the school" in line for line in summary))


class SmokeEntity(unittest.TestCase):
    def setUp(self):
        for name in ("_passed", "_failed", "_skipped"):
            self.addCleanup(setattr, smoke, name, getattr(smoke, name))
            setattr(smoke, name, 0)

    def run_check(self, env, replies=()):
        out = io.StringIO()
        with mock.patch.object(smoke, "run", side_effect=list(replies)) as run, contextlib.redirect_stdout(out):
            smoke.check_chat_entity(env)
        return out.getvalue(), run

    def test_missing_or_placeholder_fails_without_touching_the_database(self):
        for env in ({}, {"CHAT_LEGAL_ENTITY": ""}, {"CHAT_LEGAL_ENTITY": "CHANGE_ME_legal_entity_code"}):
            smoke._failed = 0
            text, run = self.run_check(env)
            self.assertEqual(smoke._failed, 1, env)
            self.assertIn("is not set", text)
            run.assert_not_called()

    def test_invalid_codes_never_reach_the_database(self):
        for value in ("X' OR '1'='1", "a;b", "a b", "Č", "../x", "x" * 65, "a\nb"):
            smoke._failed = 0
            text, run = self.run_check({"CHAT_LEGAL_ENTITY": value})
            self.assertEqual(smoke._failed, 1, repr(value))
            self.assertIn("not a legal entity code", text)
            run.assert_not_called()

    def test_an_active_entity_passes(self):
        text, run = self.run_check({"CHAT_LEGAL_ENTITY": "SYNTH_ENTITY_1"}, [proc("1\n")])
        self.assertEqual((smoke._passed, smoke._failed), (1, 0))
        self.assertIn("SYNTH_ENTITY_1", text)
        sql = run.call_args_list[0].args[0][-1]
        self.assertIn("legal_entity_code = 'SYNTH_ENTITY_1' AND is_active", sql)

    def test_an_unknown_entity_fails_and_lists_the_active_codes(self):
        text, run = self.run_check({"CHAT_LEGAL_ENTITY": "NOPE"}, [proc(""), proc("A_1, B_2\n")])
        self.assertEqual(smoke._failed, 1)
        self.assertIn("A_1, B_2", text)

    def test_a_database_error_is_a_failure_not_a_pass(self):
        text, run = self.run_check({"CHAT_LEGAL_ENTITY": "ANY"}, [proc("", rc=1, err="connection refused")])
        self.assertEqual((smoke._passed, smoke._failed), (0, 1))

    def test_a_fixed_chat_without_a_school_is_reported_as_not_configured_not_ready(self):
        text, run = self.run_check({"CHAT_ENTITY_PICKER": "false"})
        self.assertEqual((smoke._passed, smoke._failed), (0, 1))
        self.assertIn("not configured", text)
        run.assert_not_called()

    def test_an_invalid_fixed_school_still_fails_with_the_picker_off(self):
        text, _ = self.run_check({"CHAT_LEGAL_ENTITY": "NOPE", "CHAT_ENTITY_PICKER": "false"},
                                 [proc(""), proc("A_1\n")])
        self.assertEqual((smoke._passed, smoke._failed), (0, 1))


def bff(cfg=None, probe=(422, {"error": {"code": "school_required"}}), config_error=None):
    """Patches for what smoke reads from INSIDE the chat container."""
    def get(service, url, timeout=10):
        if config_error:
            raise config_error
        return cfg
    return (mock.patch.object(smoke, "_http_json_in", side_effect=get),
            mock.patch.object(smoke, "_http_post_in", return_value=probe))


AWAITING = {"entity_picker": True, "setup": "awaiting_selection", "default": "",
            "entities": [{"code": "A_1", "name": "A"}, {"code": "B_2", "name": "B"}]}


class SmokePicker(unittest.TestCase):
    def setUp(self):
        for name in ("_passed", "_failed", "_skipped"):
            self.addCleanup(setattr, smoke, name, getattr(smoke, name))
            setattr(smoke, name, 0)

    def run_check(self, env, patches, replies=()):
        out = io.StringIO()
        with mock.patch.object(smoke, "run", side_effect=list(replies)) as run, \
                patches[0], patches[1], contextlib.redirect_stdout(out):
            smoke.check_chat_entity(env)
        return out.getvalue(), run

    ENV = {"CHAT_ENTITY_PICKER": "true", "CHAT_LEGAL_ENTITY": "CHANGE_ME_legal_entity_code"}

    def test_a_waiting_picker_passes_and_the_browser_search_is_a_skip_not_a_pass(self):
        for value in ("", "CHANGE_ME_legal_entity_code"):
            smoke._passed = smoke._failed = smoke._skipped = 0
            text, run = self.run_check({**self.ENV, "CHAT_LEGAL_ENTITY": value}, bff(AWAITING))
            self.assertEqual((smoke._passed, smoke._failed, smoke._skipped), (2, 0, 1), text)
            self.assertIn("CHAT_AWAITS_SCHOOL_SELECTION", text)
            self.assertIn("[skip]", text)
            run.assert_not_called()                  # no database query: the list comes through the chat

    def test_the_server_must_refuse_a_question_without_a_school(self):
        for probe in ((200, {"answer": "x"}), (500, {"error": {"code": "chat_misconfigured"}}),
                      (422, {"error": {"code": "invalid_request"}})):
            smoke._passed = smoke._failed = smoke._skipped = 0
            self.run_check(self.ENV, bff(AWAITING, probe=probe))
            self.assertEqual(smoke._failed, 1, probe)

    def test_a_list_or_database_error_is_a_failure_not_a_waiting_state(self):
        for cfg in ({"entity_picker": True, "setup": "entities_unavailable", "entities": []},
                    {"entity_picker": True, "setup": "no_entities", "entities": []},
                    {"entity_picker": True, "setup": "awaiting_selection", "entities": []}):
            smoke._passed = smoke._failed = smoke._skipped = 0
            text, _ = self.run_check(self.ENV, bff(cfg))
            self.assertEqual((smoke._passed, smoke._failed), (0, 1), cfg)
            self.assertNotIn("CHAT_AWAITS_SCHOOL_SELECTION", text)

    def test_an_unreachable_chat_is_a_failure(self):
        _, _ = self.run_check(self.ENV, bff(config_error=OSError("down")))
        self.assertEqual((smoke._passed, smoke._failed), (0, 1))

    def test_a_chat_that_still_has_the_picker_off_or_a_default_is_a_failure(self):
        for cfg in ({"entity_picker": False, "setup": "ready", "entities": []},
                    {**AWAITING, "setup": "ready", "default": "A_1"}):
            smoke._passed = smoke._failed = smoke._skipped = 0
            self.run_check(self.ENV, bff(cfg))
            self.assertEqual(smoke._failed, 1, cfg)

    def test_an_explicit_default_must_be_active_and_offered(self):
        env = {**self.ENV, "CHAT_LEGAL_ENTITY": "A_1"}
        ready = {**AWAITING, "setup": "ready", "default": "A_1"}
        text, _ = self.run_check(env, bff(ready), [proc("1\n")])
        self.assertEqual((smoke._passed, smoke._failed), (2, 0), text)
        smoke._passed = smoke._failed = 0
        self.run_check({**env, "CHAT_LEGAL_ENTITY": "GONE"}, bff(ready), [proc(""), proc("A_1\n")])
        self.assertEqual((smoke._passed, smoke._failed), (0, 1))     # the database says inactive
        smoke._passed = smoke._failed = 0
        self.run_check({**env, "CHAT_LEGAL_ENTITY": "X' OR '1'='1"}, bff(ready))
        self.assertEqual((smoke._passed, smoke._failed), (0, 1))     # never reaches psql

    def test_a_database_error_while_checking_an_explicit_default_is_a_failure(self):
        env = {**self.ENV, "CHAT_LEGAL_ENTITY": "A_1"}
        self.run_check(env, bff(AWAITING), [proc("", rc=1, err="connection refused")])
        self.assertEqual((smoke._passed, smoke._failed), (0, 1))


class SmokeMain(unittest.TestCase):
    def test_main_runs_the_check_after_the_database_check(self):
        source = (ROOT / "scripts" / "smoke.py").read_text(encoding="utf-8")
        self.assertLess(source.index("    check_database(env)\n    check_db_roles(env)\n    check_chat_entity(env)"), source.index("check_dry_run_ingest(env)\n    check_variable"))


if __name__ == "__main__":
    unittest.main()
