"""B06 bootstrap: what the run prints (N1) and a duplicated .env line (N2 corner).

Run: python tests/test_bootstrap_output.py   (add -v for names)

Offline and synthetic, on the fake of test_bootstrap_key_safety.py. Covers the output that bootstrap itself
passes on: redact() (never fails open, even when .env cannot be read) and relay() (only the child scripts'
own line shapes get through; the body of a multi-line message, control characters and URL credentials do not).
import_flows.py and smoke.py themselves still print response bodies when run alone: that is recorded as an open,
separate task, not changed here.
"""
import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import test_bootstrap_key_safety as ks  # noqa: E402  (module, not its classes: nothing is collected twice)
import bootstrap  # noqa: E402
import create_api_key as cak  # noqa: E402
from envfile import read_env, set_env  # noqa: E402

BODY = "syn-body-secret-ffffffff"


class RedactTests(ks.Offline):
    def test_masks_what_env_marks_as_secret_by_name(self):
        text = f"a {ks.PG_PASSWORD} b {ks.FERNET} c {ks.CF_TOKEN} d {ks.OPENAI_KEY}"
        masked = bootstrap.redact(text)
        for secret in (ks.PG_PASSWORD, ks.FERNET, ks.CF_TOKEN, ks.OPENAI_KEY):
            self.assertNotIn(secret, masked)

    def test_it_keeps_masking_when_env_cannot_be_read(self):
        text = f"x {ks.PG_PASSWORD} y {ks.OLD_KEY}"
        self.assertNotIn(ks.PG_PASSWORD, bootstrap.redact(text))                 # remembers what .env held
        for label, damage in (("invalid utf-8", lambda: self.env_path.write_bytes(b"\xff\xfe\x00 not text")),
                              ("the file is gone", lambda: self.env_path.unlink()),
                              ("emptied", lambda: self.env_path.write_bytes(b""))):
            with self.subTest(label):
                damage()
                masked = bootstrap.redact(text)
                self.assertNotIn(ks.PG_PASSWORD, masked)
                self.assertNotIn(ks.OLD_KEY, masked)
                self.write_env()

    def test_a_value_that_left_env_is_still_masked(self):
        bootstrap.redact("warm up")                                              # reads the OLD key from .env
        self.write_env(LANGFLOW_API_KEY=ks.NEW_KEY)                              # the key was replaced
        masked = bootstrap.redact(f"{ks.OLD_KEY} {ks.NEW_KEY}")
        self.assertNotIn(ks.OLD_KEY, masked)
        self.assertNotIn(ks.NEW_KEY, masked)


class RelayTests(ks.Offline):
    def relayed(self, text):
        with self.capture() as out:
            bootstrap.relay(text)
        return out.stdout

    def test_the_body_of_a_multi_line_failure_is_never_relayed(self):
        out = self.relayed("  [FAIL] probe flow: HTTP 500\n  " + BODY + " second line of the body\n  third line\n"
                           "  [pass] next check\n\n5. dry-run ingest\n  [pass] gate\n")
        self.assertIn("[FAIL] probe flow (details withheld)", out)
        self.assertNotIn(BODY, out)
        self.assertNotIn("second line", out)
        self.assertNotIn("third line", out)
        self.assertIn("[pass] next check", out)
        self.assertIn("5. dry-run ingest", out)                                  # a heading ends the message
        self.assertIn("[pass] gate", out)

    def test_the_rest_of_a_multi_line_pass_or_skip_message_is_dropped_too(self):
        out = self.relayed("  [pass] reached the service\n  Got: " + BODY + "\n  [skip] no staff user\n  payload {\"k\": 1}\n")
        self.assertIn("[pass] reached the service", out)
        self.assertIn("[skip] no staff user", out)
        self.assertNotIn(BODY, out)
        self.assertNotIn("payload", out)

    def test_the_childs_own_structural_lines_are_kept(self):
        lines = ["edu_ai smoke test", "", "1. container health", "  [pass] postgres healthy", "6. variable resolution",
                 "  [skip] not configured", "", "2 passed, 0 failed, 1 skipped", "Skipped checks were not run — they are not passes.",
                 "Importing 6 flows into http://127.0.0.1:8444", "  + EDU_AI_LOAD_PDF            endpoint_name=a -> id1 (created)",
                 "  ~ EDU_AI_QA                  endpoint_name=b (updated)", "Done. Flows are addressed by endpoint_name",
                 "no env vars to paste."]
        out = self.relayed("\n".join(lines))
        for line in lines:
            if line:
                self.assertIn(line, out)

    def test_control_characters_become_question_marks(self):
        out = self.relayed("  [pass] \x1b[31mred\x1b[0m and \x07 bell\r\n")
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)
        self.assertNotIn("\r", out)
        self.assertIn("[pass] ?[31mred?[0m and ? bell", out)

    def test_credentials_inside_a_url_are_removed(self):
        out = self.relayed(f"Importing 6 flows into http://admin:{BODY}@host:7860/x\n")
        self.assertNotIn(BODY, out)
        self.assertNotIn("admin:", out)
        self.assertIn("http://host:7860/x", out)

    def test_a_known_secret_is_masked_before_the_line_is_shortened(self):
        out = self.relayed("  [pass] " + "x" * 120 + ks.OLD_KEY + " tail\n")
        self.assertNotIn(ks.OLD_KEY, out)
        self.assertNotIn(ks.OLD_KEY[:12], out)                                   # no half of it left by the cut

    def test_a_fail_line_is_cut_at_the_first_colon(self):
        out = self.relayed(f"  [FAIL] ingest: HTTP 422 {BODY}\n")
        self.assertIn("[FAIL] ingest (details withheld)", out)
        self.assertNotIn(BODY, out)


class DuplicatedLineTests(ks.Offline):
    def test_every_occurrence_of_a_duplicated_key_is_rewritten(self):
        path = self.tmp / "dup.env"
        path.write_bytes(b"A=1\nB=2\nA=3\n")
        set_env(path, {"A": "9"})
        self.assertEqual(path.read_bytes(), b"A=9\nB=2\nA=9\n")
        self.assertEqual(read_env(path)["A"], "9")                               # the last occurrence wins here and in Compose

    def test_a_new_api_key_is_stored_and_read_back_when_the_line_is_duplicated(self):
        self.write_env()
        self.env_path.write_bytes(self.env_path.read_bytes() + b"\nLANGFLOW_API_KEY=" + ks.OLD_KEY.encode() + b"\n")
        cak._store(self.env_path, ks.NEW_KEY)
        self.assertEqual(read_env(self.env_path)["LANGFLOW_API_KEY"], ks.NEW_KEY)

    def test_other_keys_comments_and_order_are_untouched(self):
        path = self.tmp / "keep.env"
        path.write_bytes(b"# head\nA=1\n\n# mid\nB=2\nA=3\n# tail\n")
        set_env(path, {"B": "22"})
        self.assertEqual(path.read_bytes(), b"# head\nA=1\n\n# mid\nB=22\nA=3\n# tail\n")


if __name__ == "__main__":
    unittest.main()
