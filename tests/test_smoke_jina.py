"""Offline: the smoke notice when the settings use Jina but the search container has no key. No Docker."""
import subprocess
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import smoke  # noqa: E402


def done(rc=0, out=""):
    return subprocess.CompletedProcess([], rc, out, "")


class JinaPrefilter(unittest.TestCase):
    def check(self, rows, key_rc):
        answers = [rows, done(key_rc)]
        with patch.object(smoke, "run", side_effect=lambda *a, **k: answers.pop(0)) as run, \
                patch.object(smoke, "ok") as ok, patch.object(smoke, "bad") as bad, patch.object(smoke, "note") as note:
            smoke.check_jina_prefilter({})
        bad.assert_not_called()                      # never a failure: the answers still come
        return run, ok, note

    def test_no_jina_in_the_settings_says_nothing(self):
        run, ok, note = self.check(done(0, "0\n"), 0)
        ok.assert_not_called(); note.assert_not_called()
        self.assertEqual(run.call_count, 1)

    def test_jina_with_a_key_passes(self):
        run, ok, note = self.check(done(0, "4\n"), 0)
        ok.assert_called_once(); note.assert_not_called()

    def test_jina_without_a_key_is_a_note_not_a_failure(self):
        run, ok, note = self.check(done(0, "4\n"), 1)
        ok.assert_not_called(); note.assert_called_once()
        self.assertIn("JINA_API_KEY is empty", note.call_args.args[0])

    def test_unreadable_settings_are_a_note(self):
        run, ok, note = self.check(done(1, ""), 0)
        note.assert_called_once()
        self.assertEqual(run.call_count, 1)

    def test_the_query_only_reads(self):
        run, ok, note = self.check(done(0, "0\n"), 0)
        sql = run.call_args_list[0].args[0][-1]
        self.assertTrue(sql.startswith("SELECT "))
        self.assertNotIn("JINA_API_KEY", " ".join(run.call_args_list[0].args[0]))


if __name__ == "__main__":
    unittest.main()
