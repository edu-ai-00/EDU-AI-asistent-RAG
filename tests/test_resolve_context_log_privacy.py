"""Formatted context-node logs exclude content; all branches retain their output.

Run in the search dependency environment: python tests/test_resolve_context_log_privacy.py.
No database, network or LLM call is made. See test_server_log_privacy.py for the
separate check that the sanctioned query log still receives the original text.
"""
from contextlib import ExitStack
import logging
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "search"), str(ROOT / "search" / "eduai_normalize")]
from app.nodes import resolve_context as rc

RAW = "a kdy __PRIVATE_RAW_9b71__?"
REWRITTEN = "__PRIVATE_REWRITE_a681__"
HISTORY = "__PRIVATE_HISTORY_d251__"
ANSWER = "__PRIVATE_RETRIEVED_c446__"
ERROR = "__PRIVATE_EXCEPTION_e224__"
SENTINELS = (RAW, REWRITTEN, HISTORY, ANSWER, ERROR)
STATE = {"user_question": RAW, "conversation_id": "synthetic-conversation",
         "legal_entity": "SYNTHETIC_ENTITY", "language_filter": "cs",
         "query_language": "cs"}
TURNS = [(HISTORY, ANSWER)]


class SensitiveFailure(RuntimeError):
    pass


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []
        self.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))

    def emit(self, record):
        self.messages.append(self.format(record))


class ContextLogPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.capture = Capture()
        self.old_level = rc.logger.level
        rc.logger.setLevel(logging.INFO)
        rc.logger.addHandler(self.capture)
        self.addCleanup(rc.logger.removeHandler, self.capture)
        self.addCleanup(rc.logger.setLevel, self.old_level)

    def assert_private(self, message):
        self.assertTrue(self.capture.messages)
        formatted = "\n".join(self.capture.messages)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, formatted)
        self.assertIn(message, formatted)

    def run_node(self, *, exact=False, covered=False, rewrite=REWRITTEN, offered=(), error=None):
        with ExitStack() as stack:
            stack.enter_context(patch.object(rc, "_needs_context", return_value=True))
            exact_mock = stack.enter_context(patch.object(rc, "_has_exact_variant", return_value=exact))
            cover_mock = stack.enter_context(patch.object(rc, "_answers_as_typed", return_value=covered))
            turns_mock = stack.enter_context(patch.object(rc, "_load_turns", return_value=TURNS))
            rewrite_mock = stack.enter_context(patch.object(rc, "_rewrite", return_value=rewrite,
                                                          side_effect=error))
            stack.enter_context(patch.object(rc.conversation, "load_seen", return_value=([], set(offered))))
            stack.enter_context(patch.object(rc, "system_message", return_value="safe static answer"))
            result = rc.resolve_context(dict(STATE), {"context": {"enabled": True}})
        exact_mock.assert_called_once_with(RAW, {"context": {"enabled": True}}, STATE)
        return result, cover_mock, turns_mock, rewrite_mock

    def test_exact_match_is_still_a_skip(self):
        result, cover, turns, rewrite = self.run_node(exact=True)
        self.assertEqual(result, {"context_skip_reason": "raw text is an exact QA match"})
        cover.assert_not_called(); turns.assert_not_called(); rewrite.assert_not_called()
        self.assert_private("reason=exact_qa_match")

    def test_covered_match_is_still_a_skip(self):
        result, _, turns, rewrite = self.run_node(covered=True)
        self.assertEqual(result, {"context_skip_reason": "raw text covers a curated question"})
        turns.assert_not_called(); rewrite.assert_not_called()
        self.assert_private("reason=qa_covered")

    def test_offered_chip_is_still_used_as_typed(self):
        result, _, _, rewrite = self.run_node(rewrite=rc._NOT_A_QUESTION, offered=[RAW.casefold()])
        self.assertEqual(result, {"context_skip_reason": "text was offered as a suggestion"})
        rewrite.assert_called_once_with(TURNS, RAW, {"enabled": True})
        self.assert_private("reason=offered_suggestion")

    def test_not_a_question_still_returns_the_static_answer(self):
        result, _, _, _ = self.run_node(rewrite=rc._NOT_A_QUESTION)
        self.assertEqual(result, {"resolved": True, "outcome": "answer", "answer_source": "none",
            "answer": "safe static answer", "suggestions": [], "sources_json": "[]",
            "user_language": "cs", "context_turns": 1})
        self.assert_private("reason=not_a_question")

    def test_previous_turn_echo_is_still_discarded(self):
        result, _, _, _ = self.run_node(rewrite=HISTORY)
        self.assertEqual(result, {"context_skip_reason": "rewrite echoed the previous question"})
        self.assert_private("reason=previous_turn_echo")

    def test_rewrite_retains_both_texts_in_state_only(self):
        result, _, _, rewrite = self.run_node()
        self.assertEqual(result, {"user_question": REWRITTEN, "raw_question": RAW, "context_turns": 1})
        rewrite.assert_called_once_with(TURNS, RAW, {"enabled": True})
        self.assert_private("rewritten turns=1")

    def failure(self):
        return SensitiveFailure(" / ".join(SENTINELS))

    def test_rewrite_failure_still_falls_through(self):
        result, _, _, _ = self.run_node(error=self.failure())
        self.assertEqual(result, {})
        self.assert_private("rewrite failed (SensitiveFailure)")

    def test_turn_lookup_failure_still_returns_no_turns(self):
        with patch.object(rc, "get_cursor", side_effect=self.failure()):
            self.assertEqual(rc._load_turns(STATE, "2 hours", 4), [])
        self.assert_private("turn lookup failed (SensitiveFailure)")

    def probe_failure(self, fn, marker):
        with ExitStack() as stack:
            stack.enter_context(patch.object(rc, "languages_for_entity", return_value=["cs"]))
            stack.enter_context(patch.object(rc, "organization_entities", return_value=["SYNTHETIC_ENTITY"]))
            stack.enter_context(patch.object(rc, "coverage_stopwords", return_value={}))
            stack.enter_context(patch.object(rc, "_union_forms", return_value={
                "norm": ["synthetic", "terms"], "lemma": ["synthetic", "terms"]}))
            cursor = stack.enter_context(patch.object(rc, "get_cursor", side_effect=self.failure()))
            self.assertIs(fn(RAW, {}, STATE), False)
            cursor.assert_called_once()
        self.assert_private(marker + " (SensitiveFailure)")

    def test_exact_probe_failure_still_falls_through(self):
        self.probe_failure(rc._has_exact_variant, "exact probe failed")

    def test_coverage_probe_failure_still_falls_through(self):
        self.probe_failure(rc._answers_as_typed, "cover probe failed")


if __name__ == "__main__":
    unittest.main()
