"""Unit tests for `offer_exclude_categories` (migration 231): entries of an excluded QA
category may be the answer but are never offered as a chip.

Pure: no DB (the category lookup and the rankings are stubbed). Run inside the search
image with this checkout's code on the path:

    docker cp search/. edu_ai_search:/tmp/oxc/ && docker cp tests/test_offer_exclude_categories.py edu_ai_search:/tmp/oxc/
    docker exec -w /tmp/oxc -e PYTHONPATH=/tmp/oxc:/tmp/oxc/eduai_normalize edu_ai_search \
        python /tmp/oxc/test_offer_exclude_categories.py

The test_* functions are also pytest-compatible.

What it protects: with the parameter absent nothing is looked up and nothing changes;
with it set, an excluded entry leaves the n-gram chip, the other-grade offer and the
chip row of an answer, and the row is topped up from the remaining entries.
"""

import os
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.nodes import qa_common as qc  # noqa: E402
from app.nodes import qa_lexical as ql  # noqa: E402
from app.nodes import qa_ngram as ng  # noqa: E402

ON = {"offer_exclude_categories": ["CONVERSATION"]}


def _items(*ids):
    return [{"kind": "qa", "entry_id": i, "question": f"q{i}", "score": 0.5} for i in ids]


class _Cur:
    """Records the one query no_offer_ids makes and returns `rows`."""
    def __init__(self, rows=()):
        self.rows, self.sql, self.args = list(rows), None, None

    def execute(self, sql, args=None):
        self.sql, self.args = sql, args

    def fetchall(self):
        return self.rows


class _Boom:
    def execute(self, *a, **k):
        raise AssertionError("database read while the parameter is off")


def test_offer_codes():
    assert qc.offer_codes(None) == []
    assert qc.offer_codes({}) == []
    assert qc.offer_codes({"offer_exclude_categories": "CONVERSATION"}) == []
    assert qc.offer_codes({"offer_exclude_categories": [" conversation ", ""]}) == ["CONVERSATION"]


def test_no_offer_ids_off_reads_nothing():
    assert qc.no_offer_ids(_Boom(), ["a", "b"], []) == set()
    assert qc.no_offer_ids(_Boom(), [], ["CONVERSATION"]) == set()


def test_no_offer_ids_query():
    cur = _Cur([("b",)])
    assert qc.no_offer_ids(cur, ["a", "b", None], ["CONVERSATION"]) == {"b"}
    assert "qa_category" in cur.sql and "category_id" in cur.sql
    assert cur.args == (["CONVERSATION"], ["a", "b"])


def _chip(ranked, params, skip):
    real_ranked, real_skip = ng.ranked_for, ng.no_offer_ids
    ng.ranked_for = lambda cur, state, opts: ranked
    ng.no_offer_ids = lambda cur, ids, codes: set(skip)
    out = {"resolved": False, "decision": "B-deferred",
           "deferred_suggest": {"suggestions": _items("a", "b", "c")}, "trace_detail": {}}
    try:
        return ng.with_ngram_chip(out, {}, {"suggest_top_n": 3, "offer_ngram": True, **params}, None)
    finally:
        ng.ranked_for, ng.no_offer_ids = real_ranked, real_skip


def test_ngram_chip_skips_an_excluded_entry():
    res = _chip([("x", "qx", 0.9), ("y", "qy", 0.6)], ON, {"x"})
    assert [c["entry_id"] for c in res["deferred_suggest"]["suggestions"]] == ["a", "b", "y"]


def test_ngram_chip_unchanged_when_off():
    res = _chip([("x", "qx", 0.9)], {}, {"x"})
    assert [c["entry_id"] for c in res["deferred_suggest"]["suggestions"]] == ["a", "b", "x"]


@contextmanager
def _no_db():
    yield None


def _wrapper(params, skip):
    real = (ql._qa_lexical_decision, ql._other_grade_suggestions, ql.no_offer_ids, ql.get_cursor)
    ql._qa_lexical_decision = lambda state, params=None: {"resolved": True, "suggestions": []}
    ql._other_grade_suggestions = lambda state, params=None: _items("g1", "g2")
    ql.no_offer_ids = lambda cur, ids, codes: set(skip)
    ql.get_cursor = _no_db
    try:
        return ql.qa_lexical({}, params)
    finally:
        ql._qa_lexical_decision, ql._other_grade_suggestions, ql.no_offer_ids, ql.get_cursor = real


def test_other_grade_offer_drops_an_excluded_entry():
    out = _wrapper(ON, {"g1"})
    assert [c["entry_id"] for c in out["suggestions"]] == ["g2"]


def test_other_grade_offer_unchanged_when_off():
    out = _wrapper({}, {"g1"})
    assert [c["entry_id"] for c in out["suggestions"]] == ["g1", "g2"]


def _finalize(parked, siblings, params, skip):
    real = (qc.packs_of, qc.category_siblings, qc.no_offer_ids, qc._scope_frags, qc.content_tokens)
    qc.packs_of = lambda cur, ids: {str(i): "p" for i in ids if i}
    qc.category_siblings = lambda *a, **k: siblings
    qc.no_offer_ids = lambda cur, ids, codes: set(skip) if codes else set()
    qc._scope_frags = lambda state: ("", [], "", [])
    qc.content_tokens = lambda state, form: []
    try:
        return qc.finalize_chips(None, {}, params, "top", parked, 3)
    finally:
        qc.packs_of, qc.category_siblings, qc.no_offer_ids, qc._scope_frags, qc.content_tokens = real


def test_answer_chips_drop_excluded_and_top_up():
    sib = [("s1", "qs1", 0.4), ("s2", "qs2", 0.3), ("s3", "qs3", 0.2)]
    chips = _finalize(_items("a", "x"), sib, ON, {"x", "s1"})
    assert [c["entry_id"] for c in chips] == ["a", "s2", "s3"]


def test_answer_chips_unchanged_when_off():
    sib = [("s1", "qs1", 0.4), ("s2", "qs2", 0.3)]
    chips = _finalize(_items("a", "x"), sib, {}, {"x", "s1"})
    assert [c["entry_id"] for c in chips] == ["a", "x", "s1"]


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
    print(f"Ran {n} tests")
    print("OK")
