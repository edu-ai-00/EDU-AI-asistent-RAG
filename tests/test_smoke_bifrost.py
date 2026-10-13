"""Offline: the smoke check that Bifrost v2 has not turned inference auth on. No Docker."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import smoke  # noqa: E402


class BifrostInferenceAuth(unittest.TestCase):
    def check(self, answer):
        side = answer if isinstance(answer, (Exception, list)) and not isinstance(answer, dict) else None
        with patch.object(smoke, "_http_json_in", side_effect=side, return_value=answer) as get, \
                patch.object(smoke, "bad") as bad, patch.object(smoke, "ok") as ok, patch.object(smoke.time, "sleep"):
            smoke.check_bifrost_inference_open()
        self.assertEqual(get.call_args.args, ("edu_ai_search", "http://bifrost:8080/api/session/is-auth-enabled"))
        return bad, ok

    def test_open_inference_passes_with_or_without_an_admin(self):
        for admin in (False, True):
            with self.subTest(admin=admin):
                bad, ok = self.check({"inference_auth_enforced": False, "is_auth_enabled": admin})
                bad.assert_not_called()
                ok.assert_called_once()

    def test_enforced_or_unknown_inference_auth_fails(self):
        for state in ({"inference_auth_enforced": True}, {}, {"inference_auth_enforced": None}):
            with self.subTest(state=state):
                bad, ok = self.check(state)
                bad.assert_called_once()
                ok.assert_not_called()
                self.assertIn("enforce auth on inference", bad.call_args.args[0])

    def test_an_unreadable_state_fails_instead_of_passing(self):
        bad, ok = self.check(OSError("exec failed"))
        bad.assert_called_once()
        ok.assert_not_called()

    def test_a_restarting_bifrost_is_retried(self):
        bad, ok = self.check([OSError("refused"), OSError("refused"), {"inference_auth_enforced": False}])
        bad.assert_not_called()
        ok.assert_called_once()

    def test_an_answer_that_is_not_an_object_fails(self):
        for answer in ([1, 2], "open", 7):
            with self.subTest(answer=answer), patch.object(smoke, "_http_json_in", return_value=answer), \
                    patch.object(smoke, "bad") as bad, patch.object(smoke, "ok") as ok:
                smoke.check_bifrost_inference_open()
            bad.assert_called_once()
            ok.assert_not_called()


if __name__ == "__main__":
    unittest.main()
