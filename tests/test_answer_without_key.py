"""Unit tests for the answer step without a chat key (search/app/nodes/answer.py, `skip_without_key`).

Pure: no DB, no model (the generation is stubbed). Run inside the search image:

    docker exec -i edu_ai_search python - < tests/test_answer_without_key.py

What it protects: an absent key or the installer's placeholder (CHANGE_ME…) counts
as "no model"; with `skip_without_key` and no model the step does not try the
generation and ends like an empty passage pool (the parked offer, else no_answer);
with a key, or with the parameter off, the step behaves as before.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import config, llm  # noqa: E402
from app.nodes import answer as an  # noqa: E402


class _Called(Exception):
    pass


def _with(key, fn):
    old_key = config.OPENAI_API_KEY
    old_prep, old_unans = an.prepare_generation, an.resolve_unanswered
    config.OPENAI_API_KEY = key

    def prep(*a, **k):
        raise _Called("generation prepared")
    an.prepare_generation = prep
    an.resolve_unanswered = lambda state: {"resolved": True, "outcome": "no_answer",
                                           "suggestions": state.get("fallback_suggestions") or []}
    try:
        return fn()
    finally:
        config.OPENAI_API_KEY = old_key
        an.prepare_generation, an.resolve_unanswered = old_prep, old_unans


STATE = {"user_question": "q", "fallback_suggestions": [{"kind": "qa", "entry_id": "e1", "question": "Q?"}]}


def test_placeholder_and_empty_mean_no_model():
    for key, want in (("", False), ("  ", False), ("CHANGE_ME", False), ("CHANGE_ME_please", False),
                      ("sk-real", True)):
        assert _with(key, llm.chat_configured) is want, key


def test_no_key_and_on_skips_the_generation():
    out = _with("CHANGE_ME", lambda: an.answer(dict(STATE), {"skip_without_key": True}))
    assert out["outcome"] == "no_answer" and out["suggestions"][0]["entry_id"] == "e1"


def test_no_key_and_off_still_tries_the_generation():
    try:
        _with("CHANGE_ME", lambda: an.answer(dict(STATE), {}))
    except _Called:
        return
    raise AssertionError("the generation was not prepared with the parameter off")


def test_key_and_on_still_generates():
    try:
        _with("sk-real", lambda: an.answer(dict(STATE), {"skip_without_key": True}))
    except _Called:
        return
    raise AssertionError("the generation was skipped although a key is set")


def test_resolved_state_is_left_alone():
    assert _with("CHANGE_ME", lambda: an.answer({"resolved": True}, {"skip_without_key": True})) == {}


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
    print(f"Ran {n} tests")
    print("OK")
