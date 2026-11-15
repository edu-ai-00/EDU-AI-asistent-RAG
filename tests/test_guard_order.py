"""Unit tests for the order of the route guard (`blocklist_before_meta`, migration 232).

Pure: no DB (the three checks are stubbed). Run inside the search image with this
checkout's code on the path:

    docker cp search/. edu_ai_search:/tmp/go/ && docker cp tests/test_guard_order.py edu_ai_search:/tmp/go/
    docker exec -w /tmp/go -e PYTHONPATH=/tmp/go:/tmp/go/eduai_normalize edu_ai_search \
        python /tmp/go/test_guard_order.py

The test_* functions are also pytest-compatible.

What it protects: without the parameter the order is unchanged (meta, identity,
blocklist); with it a blocklist hit decides first, so "umíš mi pomoct? chci se zabít"
gets the crisis answer and not the capabilities one, while a clean meta question
still gets its answer.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.nodes import route  # noqa: E402

META = {"answer_source": "meta"}
IDENTITY = {"answer_source": "meta", "trace_detail": {"guard": "identity"}}
BLOCKED = {"answer_source": "guard", "outcome": "self_harm"}
STATE = {"query_norm_tokens": ["x"], "query_lemma_tokens": ["x"]}


def _guard(params, meta=None, identity=None, blocked=None, soft=()):
    real = route._meta_check, route._identity_check, route._blocklist_check
    route._meta_check = lambda state, params=None: meta
    route._identity_check = lambda state, params=None: identity
    route._blocklist_check = lambda state, params=None: (blocked, list(soft))
    try:
        return route._guard(STATE, params)
    finally:
        route._meta_check, route._identity_check, route._blocklist_check = real


def test_off_meta_wins_over_a_blocklist_hit():
    assert _guard({}, meta=META, blocked=BLOCKED) == (META, [])


def test_on_a_blocklist_hit_wins_over_meta():
    assert _guard({"blocklist_before_meta": True}, meta=META, blocked=BLOCKED,
                  soft=[("profanity", 0)]) == (BLOCKED, [("profanity", 0)])


def test_on_a_clean_meta_question_still_gets_its_answer():
    assert _guard({"blocklist_before_meta": True}, meta=META) == (META, [])


def test_on_identity_after_a_clean_blocklist():
    assert _guard({"blocklist_before_meta": True}, identity=IDENTITY) == (IDENTITY, [])


def test_on_soft_hits_travel_with_the_meta_answer():
    assert _guard({"blocklist_before_meta": True}, meta=META,
                  soft=[("profanity", 0)]) == (META, [("profanity", 0)])


def test_nothing_hits():
    assert _guard({}) == (None, [])
    assert _guard({"blocklist_before_meta": True}) == (None, [])


def test_unusable_query_comes_first_either_way():
    for params in ({}, {"blocklist_before_meta": True}):
        out, soft = route._guard({"query_norm_tokens": [], "query_lemma_tokens": []}, params)
        assert out["outcome"] == "unusable" and soft == []


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
    print(f"Ran {n} tests")
    print("OK")
